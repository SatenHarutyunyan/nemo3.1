# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass
from typing import List

import editdistance
import torch
from torchmetrics import Metric

from nemo.collections.asr.metrics.rnnt_wer import AbstractRNNTDecoding
from nemo.collections.asr.metrics.wer import move_dimension_to_the_front
from nemo.collections.asr.parts.submodules import rnnt_beam_decoding as beam_decode
from nemo.collections.asr.parts.submodules import rnnt_greedy_decoding as greedy_decode
from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.utils import logging

__all__ = ['RNNTBPEDecoding', 'RNNTBPEWER']


class RNNTBPEDecoding(AbstractRNNTDecoding):
    """
    
    """

    def __init__(self, decoding_cfg, decoder, joint, tokenizer: TokenizerSpec):
        blank_id = tokenizer.tokenizer.vocab_size
        self.tokenizer = tokenizer

        super(RNNTBPEDecoding, self).__init__(
            decoding_cfg=decoding_cfg, decoder=decoder, joint=joint, blank_id=blank_id
        )

    def decode_tokens_to_str(self, tokens: List[int]) -> str:
        """
        Implemented by subclass in order to decoder a token list into a string.

        Args:
            tokens: List of int representing the token ids.

        Returns:
            A decoded string.
        """
        hypothesis = self.tokenizer.ids_to_text(tokens)
        return hypothesis

    def decode_ids_to_tokens(self, tokens: List[int]) -> List[str]:
        """
        Implemented by subclass in order to decode a token id list into a token list.
        A token list is the string representation of each token id.

        Args:
            tokens: List of int representing the token ids.

        Returns:
            A list of decoded tokens.
        """
        token_list = self.tokenizer.ids_to_tokens(tokens)
        return token_list


class RNNTBPEWER(Metric):
    """
    This metric computes numerator and denominator for Overall Word Error Rate (WER) between prediction and reference texts.
    When doing distributed training/evaluation the result of res=WER(predictions, targets, target_lengths) calls
    will be all-reduced between all workers using SUM operations.
    Here contains two numbers res=[wer_numerator, wer_denominator]. WER=wer_numerator/wer_denominator.

    If used with PytorchLightning LightningModule, include wer_numerator and wer_denominators inside validation_step results.
    Then aggregate (sum) then at the end of validation epoch to correctly compute validation WER.

    Example:
        def validation_step(self, batch, batch_idx):
            ...
            wer_num, wer_denom = self.__wer(predictions, transcript, transcript_len)
            return {'val_loss': loss_value, 'val_wer_num': wer_num, 'val_wer_denom': wer_denom}

        def validation_epoch_end(self, outputs):
            ...
            wer_num = torch.stack([x['val_wer_num'] for x in outputs]).sum()
            wer_denom = torch.stack([x['val_wer_denom'] for x in outputs]).sum()
            tensorboard_logs = {'validation_loss': val_loss_mean, 'validation_avg_wer': wer_num / wer_denom}
            return {'val_loss': val_loss_mean, 'log': tensorboard_logs}

    Args:
        decoding: RNNTBPEDecoding object that will perform autoregressive decoding of the RNNT model.
        batch_dim_index: Index of the batch dimension.
        use_cer: Whether to use Character Error Rate isntead of Word Error Rate.
        log_prediction: Whether to log a single decoded sample per call.

    Returns:
        res: a tuple of 3 zero dimensional float32 ``torch.Tensor` objects: a WER score, a sum of Levenstein's
            distances for all prediction - reference pairs, total number of words in all references.
    """

    def __init__(
        self,
        decoding: RNNTBPEDecoding,
        batch_dim_index=0,
        use_cer: bool = False,
        log_prediction: bool = True,
        dist_sync_on_step=False,
    ):
        super(RNNTBPEWER, self).__init__(dist_sync_on_step=dist_sync_on_step, compute_on_step=False)
        self.decoding = decoding
        self.batch_dim_index = batch_dim_index
        self.use_cer = use_cer
        self.log_prediction = log_prediction
        self.blank_id = self.decoding.blank_id
        self.tokenizer = self.decoding.tokenizer

        self.add_state("scores", default=torch.tensor(0), dist_reduce_fx='sum', persistent=False)
        self.add_state("words", default=torch.tensor(0), dist_reduce_fx='sum', persistent=False)

    def update(
        self,
        encoder_output: torch.Tensor,
        encoded_lengths: torch.Tensor,
        targets: torch.Tensor,
        target_lengths: torch.Tensor,
    ) -> torch.Tensor:
        words = 0.0
        scores = 0.0
        references = []
        with torch.no_grad():
            # prediction_cpu_tensor = tensors[0].long().cpu()
            targets_cpu_tensor = targets.long().cpu()
            targets_cpu_tensor = move_dimension_to_the_front(targets_cpu_tensor, self.batch_dim_index)
            tgt_lenths_cpu_tensor = target_lengths.long().cpu()

            # iterate over batch
            for ind in range(targets_cpu_tensor.shape[0]):
                tgt_len = tgt_lenths_cpu_tensor[ind].item()
                target = targets_cpu_tensor[ind][:tgt_len].numpy().tolist()
                reference = self.decoding.decode_tokens_to_str(target)
                references.append(reference)

            hypotheses, _ = self.decoding.rnnt_decoder_predictions_tensor(encoder_output, encoded_lengths)

        if self.log_prediction:
            logging.info(f"\n")
            logging.info(f"reference :{references[0]}")
            logging.info(f"predicted :{hypotheses[0]}")

        for h, r in zip(hypotheses, references):
            if self.use_cer:
                h_list = list(h)
                r_list = list(r)
            else:
                h_list = h.split()
                r_list = r.split()
            words += len(r_list)
            # Compute Levenshtein's distance
            scores += editdistance.eval(h_list, r_list)

        del hypotheses

        self.scores += torch.tensor(scores, device=self.scores.device, dtype=self.scores.dtype)
        self.words += torch.tensor(words, device=self.words.device, dtype=self.words.dtype)
        # return torch.tensor([scores, words]).to(predictions.device)

    def compute(self):
        wer = self.scores.float() / self.words
        return wer, self.scores.detach(), self.words.detach()


@dataclass
class RNNTBPEDecodingConfig:
    strategy: str = "greedy_batch"
    compute_hypothesis_token_set: bool = False

    # greedy decoding config
    greedy: greedy_decode.GreedyRNNTInferConfig = greedy_decode.GreedyRNNTInferConfig()

    # beam decoding config
    beam: beam_decode.BeamRNNTInferConfig = beam_decode.BeamRNNTInferConfig(beam_size=4)
