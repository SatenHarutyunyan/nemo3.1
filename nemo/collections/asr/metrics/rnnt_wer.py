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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Union

import editdistance
import torch
from torchmetrics import Metric

from nemo.collections.asr.metrics.wer import move_dimension_to_the_front
from nemo.collections.asr.parts.submodules import rnnt_beam_decoding as beam_decode
from nemo.collections.asr.parts.submodules import rnnt_greedy_decoding as greedy_decode
from nemo.collections.asr.parts.utils.rnnt_utils import Hypothesis, NBestHypotheses
from nemo.utils import logging

__all__ = ['RNNTDecoding', 'RNNTWER']


class AbstractRNNTDecoding(ABC):
    
    def __init__(self, decoding_cfg, decoder, joint, blank_id: int):
        super(AbstractRNNTDecoding, self).__init__()
        self.cfg = decoding_cfg
        self.blank_id = blank_id
        self.compute_hypothesis_token_set = self.cfg.get("compute_hypothesis_token_set", False)
        self.preserve_alignments = self.cfg.get('preserve_alignments', False)
        possible_strategies = ['greedy', 'greedy_batch', 'beam', 'tsd', 'alsd', 'maes']
        if self.cfg.strategy not in possible_strategies:
            raise ValueError(f"Decoding strategy must be one of {possible_strategies}")

        if self.cfg.strategy == 'greedy':
            self.decoding = greedy_decode.GreedyRNNTInfer(
                decoder_model=decoder,
                joint_model=joint,
                blank_index=self.blank_id,
                max_symbols_per_step=(
                    self.cfg.greedy.get('max_symbols', None) or self.cfg.greedy.get('max_symbols_per_step', None)
                ),
                preserve_alignments=self.preserve_alignments,
            )

        elif self.cfg.strategy == 'greedy_batch':
            self.decoding = greedy_decode.GreedyBatchedRNNTInfer(
                decoder_model=decoder,
                joint_model=joint,
                blank_index=self.blank_id,
                max_symbols_per_step=(
                    self.cfg.greedy.get('max_symbols', None) or self.cfg.greedy.get('max_symbols_per_step', None)
                ),
                preserve_alignments=self.preserve_alignments,
            )

        elif self.cfg.strategy == 'beam':

            self.decoding = beam_decode.BeamRNNTInfer(
                decoder_model=decoder,
                joint_model=joint,
                beam_size=self.cfg.beam.beam_size,
                return_best_hypothesis=decoding_cfg.beam.get('return_best_hypothesis', True),
                search_type='default',
                score_norm=self.cfg.beam.get('score_norm', True),
                softmax_temperature=self.cfg.beam.get('softmax_temperature', 1.0),
                preserve_alignments=self.preserve_alignments,
            )

        elif self.cfg.strategy == 'tsd':

            self.decoding = beam_decode.BeamRNNTInfer(
                decoder_model=decoder,
                joint_model=joint,
                beam_size=self.cfg.beam.beam_size,
                return_best_hypothesis=decoding_cfg.beam.get('return_best_hypothesis', True),
                search_type='tsd',
                score_norm=self.cfg.beam.get('score_norm', True),
                tsd_max_sym_exp_per_step=self.cfg.beam.get('tsd_max_sym_exp', 10),
                softmax_temperature=self.cfg.beam.get('softmax_temperature', 1.0),
                preserve_alignments=self.preserve_alignments,
            )

        elif self.cfg.strategy == 'alsd':

            self.decoding = beam_decode.BeamRNNTInfer(
                decoder_model=decoder,
                joint_model=joint,
                beam_size=self.cfg.beam.beam_size,
                return_best_hypothesis=decoding_cfg.beam.get('return_best_hypothesis', True),
                search_type='alsd',
                score_norm=self.cfg.beam.get('score_norm', True),
                alsd_max_target_len=self.cfg.beam.get('alsd_max_target_len', 2),
                softmax_temperature=self.cfg.beam.get('softmax_temperature', 1.0),
                preserve_alignments=self.preserve_alignments,
            )

        elif self.cfg.strategy == 'maes':
            self.decoding = beam_decode.BeamRNNTInfer(
                decoder_model=decoder,
                joint_model=joint,
                beam_size=self.cfg.beam.beam_size,
                return_best_hypothesis=decoding_cfg.beam.get('return_best_hypothesis', True),
                search_type='maes',
                score_norm=self.cfg.beam.get('score_norm', True),
                maes_num_steps=self.cfg.beam.get('maes_num_steps', 2),
                maes_prefix_alpha=self.cfg.beam.get('maes_prefix_alpha', 1),
                maes_expansion_gamma=self.cfg.beam.get('maes_expansion_gamma', 2.3),
                maes_expansion_beta=self.cfg.beam.get('maes_expansion_beta', 2.0),
                softmax_temperature=self.cfg.beam.get('softmax_temperature', 1.0),
                preserve_alignments=self.preserve_alignments,
            )

        else:

            raise ValueError(
                f"Incorrect decoding strategy supplied. Must be one of {possible_strategies}\n"
                f"but was provided {self.cfg.strategy}"
            )

    def rnnt_decoder_predictions_tensor(
        self,
        encoder_output: torch.Tensor,
        encoded_lengths: torch.Tensor,
        return_hypotheses: bool = False,
        partial_hypotheses: Optional[List[Hypothesis]] = None,
    ) -> (List[str], Optional[List[List[str]]], Optional[Union[Hypothesis, NBestHypotheses]]):
        """
        Decode an encoder output by autoregressive decoding of the Decoder+Joint networks.

        Args:
            encoder_output: torch.Tensor of shape [B, D, T].
            encoded_lengths: torch.Tensor containing lengths of the padded encoder outputs. Shape [B].
            return_hypotheses: bool. If set to True it will return list of Hypothesis or NBestHypotheses

        Returns:
            If `return_best_hypothesis` is set:
                A tuple (hypotheses, None):
                hypotheses - list of Hypothesis (best hypothesis per sample).
                    Look at rnnt_utils.Hypothesis for more information.

            If `return_best_hypothesis` is not set:
                A tuple(hypotheses, all_hypotheses)
                hypotheses - list of Hypothesis (best hypothesis per sample).
                    Look at rnnt_utils.Hypothesis for more information.
                all_hypotheses - list of NBestHypotheses. Each NBestHypotheses further contains a sorted
                    list of all the hypotheses of the model per sample.
                    Look at rnnt_utils.NBestHypotheses for more information.
        """
        # Compute hypotheses
        with torch.no_grad():
           
            hypotheses_list = self.decoding(
                encoder_output=encoder_output, encoded_lengths=encoded_lengths, partial_hypotheses=partial_hypotheses
            )  # type: [List[Hypothesis]]
            # extract the hypotheses
            hypotheses_list = hypotheses_list[0]  # type: List[Hypothesis]

        prediction_list = hypotheses_list

        if isinstance(prediction_list[0], NBestHypotheses):
            hypotheses = []
            all_hypotheses = []
            for nbest_hyp in prediction_list:  # type: NBestHypotheses
                n_hyps = nbest_hyp.n_best_hypotheses  # Extract all hypotheses for this sample
                decoded_hyps = self.decode_hypothesis(n_hyps)  # type: List[str]
                hypotheses.append(decoded_hyps[0])  # best hypothesis
                all_hypotheses.append(decoded_hyps)
            if return_hypotheses:
                return hypotheses, all_hypotheses
            best_hyp_text = [h.text for h in hypotheses]
            all_hyp_text = [h.text for hh in all_hypotheses for h in hh]
            return best_hyp_text, all_hyp_text
        else:
            hypotheses = self.decode_hypothesis(prediction_list)  # type: List[str]
            if return_hypotheses:
                return hypotheses, None
            best_hyp_text = [h.text for h in hypotheses]
            return best_hyp_text, None

    def decode_hypothesis(self, hypotheses_list: List[Hypothesis]) -> List[Union[Hypothesis, NBestHypotheses]]:
        """
        Decode a list of hypotheses into a list of strings.

        Args:
            hypotheses_list: List of Hypothesis.

        Returns:
            A list of strings.
        """
        for ind in range(len(hypotheses_list)):
            # Extract the integer encoded hypothesis
            prediction = hypotheses_list[ind].y_sequence
            if type(prediction) != list:
                prediction = prediction.tolist()

            # RNN-T sample level is already preprocessed by implicit CTC decoding
            # Simply remove any blank tokens
            prediction = [p for p in prediction if p != self.blank_id]
            # print('nemo class')
          
            # De-tokenize the integer tokens
            # print('prediction',prediction)
            hypothesis = self.decode_tokens_to_str(prediction)
            # print('hypothesis',hypothesis)
            hypotheses_list[ind].text = hypothesis
            if self.compute_hypothesis_token_set:
                hypotheses_list[ind].tokens = self.decode_ids_to_tokens(prediction)
        return hypotheses_list

    @abstractmethod
    def decode_tokens_to_str(self, tokens: List[int]) -> str:
        """
        Implemented by subclass in order to decoder a token id list into a string.

        Args:
            tokens: List of int representing the token ids.

        Returns:
            A decoded string.
        """
        raise NotImplementedError()

    @abstractmethod
    def decode_ids_to_tokens(self, tokens: List[int]) -> List[str]:
        """
        Implemented by subclass in order to decode a token id list into a token list.
        A token list is the string representation of each token id.

        Args:
            tokens: List of int representing the token ids.

        Returns:
            A list of decoded tokens.

        """
      
        raise NotImplementedError()


class RNNTDecoding(AbstractRNNTDecoding):
    

    def __init__(
        self, decoding_cfg, decoder, joint, vocabulary,
    ):
        blank_id = len(vocabulary)
        self.labels_map = dict([(i, vocabulary[i]) for i in range(len(vocabulary))])

        super(RNNTDecoding, self).__init__(decoding_cfg=decoding_cfg, decoder=decoder, joint=joint, blank_id=blank_id)

    def decode_tokens_to_str(self, tokens: List[int]) -> str:
        """
        Implemented by subclass in order to decoder a token list into a string.

        Args:
            tokens: List of int representing the token ids.

        Returns:
            A decoded string.
        """
        hypothesis = ''.join(self.decode_ids_to_tokens(tokens))
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
        token_list = [self.labels_map[c] for c in tokens if c != self.blank_id]
        return token_list


class RNNTWER(Metric):
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
        decoding: RNNTDecoding object that will perform autoregressive decoding of the RNNT model.
        batch_dim_index: Index of the batch dimension.
        use_cer: Whether to use Character Error Rate isntead of Word Error Rate.
        log_prediction: Whether to log a single decoded sample per call.

    Returns:
        res: a tuple of 3 zero dimensional float32 ``torch.Tensor` objects: a WER score, a sum of Levenstein's
            distances for all prediction - reference pairs, total number of words in all references.
    """

    def __init__(
        self, decoding: RNNTDecoding, batch_dim_index=0, use_cer=False, log_prediction=True, dist_sync_on_step=False
    ):
        super(RNNTWER, self).__init__(dist_sync_on_step=dist_sync_on_step, compute_on_step=False)
        self.decoding = decoding
        self.batch_dim_index = batch_dim_index
        self.use_cer = use_cer
        self.log_prediction = log_prediction
        self.blank_id = self.decoding.blank_id
        self.labels_map = self.decoding.labels_map

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

        self.scores += torch.tensor(scores, device=self.scores.device, dtype=self.scores.dtype)
        self.words += torch.tensor(words, device=self.words.device, dtype=self.words.dtype)
        # return torch.tensor([scores, words]).to(predictions.device)

    def compute(self):
        wer = self.scores.float() / self.words
        return wer, self.scores.detach(), self.words.detach()


@dataclass
class RNNTDecodingConfig:
    strategy: str = "greedy_batch"
    compute_hypothesis_token_set: bool = False

    # greedy decoding config
    greedy: greedy_decode.GreedyRNNTInferConfig = greedy_decode.GreedyRNNTInferConfig()

    # beam decoding config
    beam: beam_decode.BeamRNNTInferConfig = beam_decode.BeamRNNTInferConfig(beam_size=4)
