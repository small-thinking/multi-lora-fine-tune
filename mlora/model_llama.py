from mlora.modelargs import LLMModelArgs, MultiLoraBatchData
from mlora.checkpoint import CheckpointRecomputeFunction
from mlora.model import repeat_kv, apply_rotary_emb, precompute_rope_angle, precompute_mask
from mlora.model import LLMModel, RMSNorm
from mlora.LoraLiner import Linear, Lora

import logging
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import xformers.ops
import xformers.ops.fmha.attn_bias
from transformers import LlamaForCausalLM
from typing import List, Dict, Tuple, Optional
from collections import OrderedDict


class Embedding(torch.nn.Module):
    def __init__(self, embedding: torch.Tensor, pad_token: int):
        super().__init__()
        self.token_embedding_: torch.Tensor = embedding
        self.padding_idx_: int = pad_token

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        data = F.embedding(tokens, self.token_embedding_,
                           padding_idx=self.padding_idx_)
        return data


class OutputLayer(torch.nn.Module):
    def __init__(self, weight: torch.Tensor):
        super().__init__()
        self.weight_: torch.Tensor = weight

    def forward(self, data: torch.Tensor) -> torch.Tensor:
        return data @ self.weight_.transpose(0, 1)


class RMSNormLayer(torch.nn.Module):
    def __init__(self, weight: torch.Tensor, eps: float = 1e-6):
        super().__init__()
        self.norm_eps_ = eps
        self.weight_ = weight

    def _norm(self, data: torch.Tensor) -> torch.Tensor:
        return data * torch.rsqrt(+ self.norm_eps_)

    def forward(self, data: torch.Tensor) -> torch.Tensor:
        input_dtype = data.dtype
        v = data.to(torch.float32).pow(2).mean(-1, keepdim=True)
        data = data * torch.rsqrt(v + self.norm_eps_)

        return (self.weight_ * data).to(input_dtype)


class Transformer(torch.nn.Module):
    def __init__(self, layer_id: int, args: LLMModelArgs):
        super().__init__()
        # attention
        self.wq_: Linear = None  # dim * dim
        self.wk_: Linear = None  # dim * dim
        self.wv_: Linear = None  # dim * dim
        self.wo_: Linear = None  # dim * dim
        # feed forward
        self.w1_: Linear = None  # also gate FNN * dim
        self.w2_: Linear = None  # also down dim * FNN
        self.w3_: Linear = None  # also up   FNN * dim
        # norm
        self.attention_norm_: RMSNorm = None  # dim
        self.ffn_norm_: RMSNorm = None        # dim
        # other arg
        self.layer_id_ = layer_id
        self.norm_eps_ = args.norm_eps_
        self.n_heads_ = args.n_heads_
        self.n_kv_heads_ = args.n_kv_heads_
        self.n_rep_ = self.n_heads_ // self.n_kv_heads_
        self.head_dim_ = args.dim_ // args.n_heads_

    @property
    def linear_layer_name_to_module_dict(self) -> Dict[str, Linear]:
        # warnning: do not use the function when not init the linear layer
        ret = {"k_proj": self.wk_,
               "q_proj": self.wq_,
               "v_proj": self.wv_,
               "o_proj": self.wo_,
               "w1_proj": self.w1_,
               "w2_proj": self.w2_,
               "w3_proj": self.w3_}
        assert all([isinstance(layer, Linear) for _, layer in ret.items()])
        return ret

    def lora_layer_name(self,
                        name: str,
                        is_lora_a: bool = False,
                        is_lora_b: bool = False) -> str:
        assert is_lora_a ^ is_lora_b

        LORA_A_NAME_STR = "lora_A"
        LORA_B_NAME_STR = "lora_B"
        lora_layer_name_fmt = "base_model.model.model.layers.{}.self_attn.{}.{}.weight"

        return lora_layer_name_fmt.format(self.layer_id_, name, LORA_A_NAME_STR if is_lora_a else LORA_B_NAME_STR)

    def from_pretrained(self,
                        transformer_layer: torch.nn.Module,
                        norm_eps: float,
                        device: torch.device) -> None:
        linear_dict_name = {"wq_": transformer_layer.self_attn.q_proj,
                            "wk_": transformer_layer.self_attn.k_proj,
                            "wv_": transformer_layer.self_attn.v_proj,
                            "wo_": transformer_layer.self_attn.o_proj,
                            "w1_": transformer_layer.mlp.gate_proj,
                            "w2_": transformer_layer.mlp.down_proj,
                            "w3_": transformer_layer.mlp.up_proj}
        norm_dict_name = {"attention_norm_": transformer_layer.input_layernorm.weight,
                          "ffn_norm_": transformer_layer.post_attention_layernorm.weight}

        for var_dict_name, source in linear_dict_name.items():
            self.__dict__[var_dict_name] = Linear(source, device=device)

        for var_dict_name, source in norm_dict_name.items():
            self.__dict__[var_dict_name] = RMSNorm(
                source.to(device=device).detach().requires_grad_(False), norm_eps)

    def init_lora_layer_weight(self,
                               adapter_name: str,
                               r: int,
                               lora_alpha: int,
                               lora_dropout: float,
                               target: Dict[str, bool],
                               weight: Optional[Dict[str, torch.Tensor]]):
        # init the lora layer, if the weight state dict have already
        # exist this lora weight, use it, otherwise init it with zero
        name_module_dict: Dict[str,
                               Linear] = self.linear_layer_name_to_module_dict

        for name, module in name_module_dict.items():
            assert isinstance(module, Linear)

            if name in target and target[name]:
                lora_weight = (None, None)
                lora_a_name = self.lora_layer_name(name, is_lora_a=True)
                lora_b_name = self.lora_layer_name(name, is_lora_b=True)

                if weight is not None and lora_a_name in weight:
                    assert lora_b_name in weight, f"can not found the layer {lora_b_name} in model."
                    lora_weight = (weight[lora_a_name], weight[lora_b_name])

                # init the lora layer
                module.init_lora_weight(
                    adapter_name, r, lora_alpha, lora_dropout, lora_weight)

    def forward(self,
                data: torch.Tensor,
                mask: torch.Tensor,
                rope_angle: Tuple[torch.Tensor, torch.Tensor],
                input_args: MultiLoraBatchData):
        batch_size, max_seq_len, _ = data.shape

        attention_norm_data = self.attention_norm_.forward(data)

        xq = self.wq_.forward(attention_norm_data, input_args)
        xk = self.wk_.forward(attention_norm_data, input_args)
        xv = self.wv_.forward(attention_norm_data, input_args)

        # conver shape to multi head
        xq = xq.view(batch_size, max_seq_len, self.n_heads_, self.head_dim_)
        xk = xk.view(batch_size, max_seq_len, self.n_kv_heads_, self.head_dim_)
        xv = xv.view(batch_size, max_seq_len, self.n_kv_heads_, self.head_dim_)

        # apply rotary embedding
        xq, xk = apply_rotary_emb(xq, xk, rope_angle)

        # for llama2 need to repeat the heads
        # before dim: batch_size, seq_len, n_kv_head, head_dim
        # after dim: batch_size, seq_len, n_head, head_dim
        xk = repeat_kv(xk, self.n_rep_)
        xv = repeat_kv(xv, self.n_rep_)

        attention_score = xformers.ops.memory_efficient_attention(
            xq, xk, xv, mask)
        attention_score = attention_score.view(batch_size, max_seq_len, -1)

        # get output attention score
        data = data + self.wo_.forward(attention_score, input_args)

        # feed forward fully connected
        score_norm_data = self.ffn_norm_.forward(data)
        w1 = self.w1_.forward(score_norm_data, input_args)
        w3 = self.w3_.forward(score_norm_data, input_args)

        data = data + self.w2_.forward(F.silu(w1) * w3, input_args)

        return data


class LlamaSequentialWrapper(torch.nn.Module):
    def __init__(self, module: torch.nn.Module):
        super().__init__()
        self.wrapper_module_ = module

    def name(self) -> str:
        return type(self.wrapper_module_).__name__

    def forward(self, input: Tuple) -> Tuple:
        assert isinstance(input, Tuple)
        assert len(input) == 5

        module_name = self.name()

        if module_name == "Embedding":
            output = self.wrapper_module_.forward(input[0])
            if input[-1]:
                output = output.requires_grad_(True)
            return (output, ) + input[1:]
        elif module_name == "Transformer":
            if input[-1]:
                output = CheckpointRecomputeFunction.apply(
                    self.wrapper_module_.forward, *input[:-1])
            else:
                output = self.wrapper_module_.forward(*input[:-1])
            return (output, ) + input[1:]
        elif module_name == "RMSNormLayer" or module_name == "OutputLayer":
            output = self.wrapper_module_.forward(input[0])
            return (output, ) + input[1:]
        else:
            raise f"module invalid: {module_name}"


class LlamaModel(LLMModel):
    def __init__(self, args: LLMModelArgs):
        # weight
        self.token_embedding_: Embedding = None

        self.layers_: List[Transformer] = []
        for layer_id in range(args.n_layers_):
            self.layers_.append(Transformer(layer_id, args))

        self.norm_: RMSNormLayer = None    # dim
        self.output_: OutputLayer = None   # vocab size * dim

        # sequential model
        self.seq_module_: torch.nn.Sequential = None

        # cos and sin
        self.rope_angle_: Tuple[torch.Tensor, torch.Tensor] = precompute_rope_angle(
            args.dim_ // args.n_heads_, args.max_seq_len_, args.device)

        self.norm_eps_ = args.norm_eps_

        self.device_ = args.device
        self.n_heads_ = args.n_heads_
        self.vocab_size_ = args.vocab_size_
        self.pad_token_id_ = args.pad_token_id_
        self.dim_ = args.dim_

        # need to set
        self.eos_token_id_ = -1

    # train model or inference model: output is probs
    def forward(self, input: MultiLoraBatchData) -> torch.Tensor:
        tokens = torch.tensor(input.batch_tokens_,
                              dtype=torch.int64).to(self.device_)

        mask = precompute_mask(tokens, self.n_heads_,
                               self.device_, input.additional_mask_)

        if input.inference_model_:
            data = (tokens, mask, self.rope_angle_, input, False)
        else:
            data = (tokens, mask, self.rope_angle_, input, True)

        for seq_layer in self.seq_module_:
            data = seq_layer.forward(data)

        return data[0]

    def init_lora_weight(self,
                         adapter_name: str,
                         r: int,
                         lora_alpha: int,
                         lora_dropout: float,
                         target: Dict[str, bool],
                         weight: Optional[Dict[str, torch.Tensor]]):
        for transformer_layer in self.layers_:
            transformer_layer.init_lora_layer_weight(
                adapter_name, r, lora_alpha, lora_dropout, target, weight)

    def from_pretrained(path: str,
                        device: str,
                        bits: int = None,
                        fp16: bool = True,
                        bf16: bool = True,
                        double_quant: bool = True,
                        quant_type: str = 'nf4') -> LLMModel:
        if bits in [4, 8]:
            logging.info('Loading model with quantization, bits = %i' % bits)
            from transformers import BitsAndBytesConfig
            compute_dtype = (torch.float16 if fp16 else (
                torch.bfloat16 if bf16 else torch.float32))
            llama_model = LlamaForCausalLM.from_pretrained(
                path,
                load_in_4bit=bits == 4,
                load_in_8bit=bits == 8,
                device_map=device,
                quantization_config=BitsAndBytesConfig(
                    load_in_4bit=bits == 4,
                    load_in_8bit=bits == 8,
                    llm_int8_threshold=6.0,
                    llm_int8_has_fp16_weight=False,
                    bnb_4bit_compute_dtype=compute_dtype,
                    bnb_4bit_use_double_quant=double_quant,
                    bnb_4bit_quant_type=quant_type,
                ),
                torch_dtype=(torch.float32 if fp16 else (torch.bfloat16 if bf16 else torch.float32)))
        else:
            llama_model = LlamaForCausalLM.from_pretrained(
                path,
                device_map=device,
                torch_dtype=torch.float32)

        llama_args = LLMModelArgs()
        llama_args.dim_ = llama_model.config.hidden_size
        llama_args.n_heads_ = llama_model.config.num_attention_heads
        llama_args.n_kv_heads_ = llama_args.n_heads_ if not hasattr(
            llama_model.config, "num_key_value_heads") else llama_model.config.num_key_value_heads
        llama_args.n_layers_ = llama_model.config.num_hidden_layers
        llama_args.norm_eps_ = llama_model.config.rms_norm_eps
        llama_args.vocab_size_ = llama_model.config.vocab_size
        llama_args.max_seq_len_ = 4096 if not hasattr(
            llama_model.config, "max_sequence_length") else llama_model.config.max_sequence_length
        llama_args.pad_token_id_ = -1
        llama_args.device = device

        model = LlamaModel(llama_args)

        # plm - pretrained large model
        def get_tensor_from_plm(name: str) -> torch.Tensor:
            origin_weight_map = {"embedding": llama_model.model.embed_tokens.weight,
                                 "norm": llama_model.model.norm.weight,
                                 "output": llama_model.lm_head.weight}
            assert name in origin_weight_map
            origin_weight = origin_weight_map[name]
            assert isinstance(origin_weight, torch.Tensor)
            return origin_weight.to(device=device).detach().requires_grad_(False)

        model.token_embedding_ = Embedding(
            get_tensor_from_plm("embedding"), llama_args.pad_token_id_)

        for idx, target_layer in enumerate(llama_model.model.layers):
            assert isinstance(model.layers_[idx], Transformer)
            target_transformer: Transformer = model.layers_[idx]
            target_transformer.from_pretrained(
                target_layer, model.norm_eps_, device=device)

        model.norm_ = RMSNormLayer(
            get_tensor_from_plm("norm"), model.norm_eps_)

        model.output_ = OutputLayer(get_tensor_from_plm("output"))

        model.seq_module_ = model.sequential_module()

        return model

    def get_train_paramas(self) -> Dict[str, List[torch.Tensor]]:
        # warnning: this will return all the lora's parameters

        # the lora layer inside the linear layer
        all_linear_layer_name = ["wq_", "wk_",
                                 "wv_", "wo_", "w1_", "w2_", "w3_"]

        def get_all_linear_layer(layer: Transformer):
            assert isinstance(layer, Transformer), f"error type {type(layer)}"
            # all linear layer from this transformer layer
            all_linear_layer: List[Linear] = [layer.__dict__[linear_layer_name]
                                              for linear_layer_name in all_linear_layer_name]
            return all_linear_layer

        def get_all_loras_layer(layer: Linear):
            assert isinstance(layer, Linear), f"error type {type(layer)}"
            # all lora adapter from this linear layer
            return layer.loras_

        all_linear_layer = [linear_layer
                            for transformer_layer in self.layers_
                            for linear_layer in get_all_linear_layer(transformer_layer)]

        all_loras_layer: List[Dict[str, Lora]] = [get_all_loras_layer(
            linear_layer) for linear_layer in all_linear_layer]

        train_paramas = {}
        for loras in all_loras_layer:
            for adatper_name, lora in loras.items():
                if adatper_name not in train_paramas:
                    train_paramas[adatper_name] = []
                train_paramas[adatper_name].extend(
                    (lora.lora_a_, lora.lora_b_))

        return train_paramas

    def get_lora_weight_dict(self, lora_name: str) -> Tuple[Dict[str, torch.Tensor], List[str]]:
        # return the lora weight dict and target lora module's name
        #   for example, lora_weight_dict = {"self_atten.q_proj.lora_A.weight", tensor}
        #                target_modules   = ["q_proj", "k_proj"]
        lora_weight_dict = {}
        target_modules = []

        # each transformer layer
        for transformer_layer in self.layers_:
            name_module_dict: Dict[str,
                                   Linear] = transformer_layer.linear_layer_name_to_module_dict
            # each linear layer in transformer layer
            for name, module in name_module_dict.items():
                loras: Dict[str, Lora] = module.loras_
                if lora_name not in loras:
                    continue
                if name not in target_modules:
                    target_modules.append(name)
                lora: Lora = loras[lora_name]
                lora_weight_dict[transformer_layer.lora_layer_name(
                    lora_name, is_lora_a=True)] = lora.lora_a_
                lora_weight_dict[transformer_layer.lora_layer_name(
                    lora_name, is_lora_b=True)] = lora.lora_b_

        return lora_weight_dict, target_modules

    def sequential_module(self) -> torch.nn.Sequential:
        seq_module = OrderedDict()

        # must ensure the follow order
        seq_module.update(
            {"embedding": LlamaSequentialWrapper(self.token_embedding_)})

        for index, layer in enumerate(self.layers_):
            layer_name = f"layer{index}"
            seq_module.update({layer_name: LlamaSequentialWrapper(layer)})

        seq_module.update({"norm": LlamaSequentialWrapper(self.norm_)})

        seq_module.update({"output": LlamaSequentialWrapper(self.output_)})

        return torch.nn.Sequential(seq_module)
