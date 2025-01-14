from mlora.modelargs import MultiLoraBatchData

import math
import torch
import torch.nn.functional as F
import bitsandbytes

from typing import Dict, Optional, Tuple


class Lora():
    def __init__(self, adapter_name: str):
        self.adapter_name_: str = adapter_name

        self.lora_a_: torch.Tensor = None
        self.lora_b_: torch.Tensor = None

        self.r_: int = 0
        self.alpha_: int = 0
        self.dropout_: float = 0.0
        self.scaling_: float = 0.0

    def set_parameter(self, r: int, alpha: int, dropout: float):
        self.r_ = r
        self.alpha_ = alpha
        self.dropout_ = dropout
        self.scaling_ = alpha / r

    def forward(self, data: torch.Tensor) -> torch.Tensor:
        data_ = F.dropout(data, self.dropout_)
        data_ @= self.lora_a_.transpose(0, 1)
        data_ @= self.lora_b_.transpose(0, 1)
        data_ *= self.scaling_
        return data_


class Linear():
    # the weight just wrapper the module from LlamaForCausalLM
    def __init__(self, weight: torch.nn.Module, device: str = None):
        if device is None:
            self.device_ = weight.device
        else:
            self.device_ = device

        if not isinstance(weight, torch.nn.Linear):
            import bitsandbytes
            assert isinstance(weight,
                              bitsandbytes.nn.Linear8bitLt) or isinstance(weight,
                                                                          bitsandbytes.nn.Linear4bit), "error type."
        else:
            weight.requires_grad_(False)

        self.weight_ = weight
        self.weight_.to(device)
        self.enable_lora_: bool = False
        self.loras_: Dict[str, Lora] = {}

    def init_lora_weight(self,
                         adapter_name: str,
                         r: int,
                         alpha: int,
                         dropout: float,
                         lora_tensor: Tuple[Optional[torch.Tensor],
                                            Optional[torch.Tensor]] = (None, None)):
        # if the lora_tensor is not (None, None), use it to init the lora weight
        assert isinstance(lora_tensor, Tuple)
        assert len(lora_tensor) == 2
        assert ((lora_tensor[0] is None) and (lora_tensor[1] is None)) or (isinstance(
            lora_tensor[0], torch.Tensor) and isinstance(lora_tensor[1], torch.Tensor))

        if adapter_name not in self.loras_:
            self.loras_[adapter_name] = Lora(adapter_name)
        self.loras_[adapter_name].set_parameter(r, alpha, dropout)

        if isinstance(self.weight_, bitsandbytes.nn.Linear4bit):
            out_dim, in_dim = self.weight_.out_features, self.weight_.in_features
        else:
            out_dim, in_dim = self.weight_.weight.shape

        def random_init_lora_a_tensor(lora: Lora):
            lora.__dict__["lora_a_"] = torch.zeros(
                size=(r, in_dim), device=self.device_, requires_grad=True, dtype=torch.float32)
            torch.nn.init.kaiming_normal_(lora.lora_a_, a=math.sqrt(5))

        def zero_init_lora_b_tensor(lora: Lora):
            lora.__dict__["lora_b_"] = torch.zeros(
                size=(out_dim, r), device=self.device_, requires_grad=True, dtype=torch.float32)

        def replace_init_lora_tensor(lora: Lora, lora_a: torch.Tensor, lora_b: torch.Tensor):
            lora.__dict__["lora_a_"] = lora_a.to(device=self.device_).to(
                torch.float32).detach().requires_grad_(True)
            lora.__dict__["lora_b_"] = lora_b.to(device=self.device_).to(
                torch.float32).detach().requires_grad_(True)

        # ensuer it's none, so we can use the __dict__ to init it
        assert self.loras_[adapter_name].lora_a_ is None
        assert self.loras_[adapter_name].lora_b_ is None

        if lora_tensor == (None, None):
            random_init_lora_a_tensor(self.loras_[adapter_name])
            zero_init_lora_b_tensor(self.loras_[adapter_name])
        else:
            replace_init_lora_tensor(self.loras_[adapter_name], *lora_tensor)

        self.enable_lora_ = True

    def forward(self, data: torch.Tensor, input_args: MultiLoraBatchData) -> torch.Tensor:
        # data shape is: batch_size * max_seq_len * dim
        # result = data @ self.weight_.transpose(0, 1)
        result = self.weight_.forward(data)

        if not self.enable_lora_:
            return result

        for lora_config in input_args.lora_batch_data_config_:
            adapter_name = lora_config.adapter_name_
            start_idx = lora_config.batch_start_idx_
            end_idx = lora_config.batch_end_idx_

            if adapter_name == "" or adapter_name not in self.loras_:
                continue

            result[start_idx: end_idx] += self.loras_[
                adapter_name].forward(data[start_idx:end_idx])

        return result
