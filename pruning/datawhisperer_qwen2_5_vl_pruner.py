"""
Author: Benhao Huang
Date: 2025-06-30
Description: This is the DataWhisper runner based on Qwen2.5-VL model.
"""

import os
import re
import torch
import uuid
import logging
import json
from sklearn.model_selection import KFold
from typing import List, Dict, Any, Optional
from PIL import Image
from argparse import Namespace
from tqdm.auto import tqdm
import matplotlib.pyplot as plt
import numpy as np
from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor
from qwen_vl_utils import process_vision_info
from accelerate import Accelerator
from utils.utils import save_json
from metrics.metric import METRICS
from prompt import DATASET_PROMPTS, format_qwenvl_message_to_qa
from pruner import Pruner

class DataWhisperer_Qwen2_5VL_Pruner(Pruner):
    def __init__(self, args: Any) -> None:
        self.args = args
        self.accelerator = Accelerator()
        
        # Setup logging
        self.logger = logging.getLogger(self.__class__.__name__)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            
        # Set logging level from args if available, otherwise INFO
        log_level = getattr(args, 'log_level', 'INFO').upper()
        self.logger.setLevel(getattr(logging, log_level, logging.INFO))
        
        # Generate unique ID for this run
        self.unique_id = str(uuid.uuid4())[:8]
        self.logger.info(f"Initializing DataWhisperer Pruner with unique ID: {self.unique_id}")
        
        self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            self.args.model_path, torch_dtype=torch.bfloat16
        )
        self.processor = AutoProcessor.from_pretrained(self.args.model_path)
        self.tokenizer = self.processor.tokenizer

        # self.model = self.accelerator.prepare(self.model)
        self.model = self.accelerator.prepare_model(self.model, evaluation_mode=True)
        if hasattr(self.model, "module"):
            self.model = self.model.module
        self.model.eval()

        self.dataset = self.args.dataset

    def generate_demonstrations(self, train_set, selected_indices, prompt_template):
        demonstrations = ""
        demo_list = []
        for idx in selected_indices:
            example = train_set[idx]
            qa_pair = prompt_template(example)[0]  # "we only want one round of conversation"
            demonstration = qa_pair[0] + "\n" + qa_pair[1]
            image_path = qa_pair[2]
            demo_list.append((demonstration, image_path))
            demonstrations += demonstration

        return demonstrations, demo_list

    def extract_predictions(self, responses_section):
        """
        Extract answers in the format: Question i: <answer text>
        If no such formatted answers are found, returns the entire response.
        """
        predictions = []
        pattern_qa = (
            r"Question\s+(\d+):\s*"   # Question number and colon
            r"(.*?)"                  # Non-greedy match for answer
            r"(?=\n\s*\n|$)"          # Until the next blank line or end of text
        )

        matches_qa = re.findall(pattern_qa, responses_section, re.DOTALL | re.IGNORECASE)

        if matches_qa:
            predictions.extend(answer.strip() for _, answer in matches_qa)
        else:
            predictions.append(responses_section.strip())

        return predictions

    def predict_batch(
        self,
        batch_val_samples: List[List[Dict[str, str]]],
        batch_demo_list: List[List[str]],
        return_attention_scores: bool = False,
    ) -> List[List[str]]:
        prompts = []
        batch_images = []
        prompts_comp = []

        for demonstration_pairs, val_samples in zip(batch_demo_list, batch_val_samples):
            prompt, images, prompt_comp = self._prepare_model_inputs(demonstration_pairs, val_samples)
            if prompt is None:
                prompts.append("")
                batch_images.append([])
                prompts_comp.append(("", "", ""))
                continue
            
            prompts.append(prompt)
            batch_images.append(images)
            prompts_comp.append(prompt_comp)

        # Generate in batch
        with torch.no_grad():
            # Tokenize the prompts in batch
            encoding = self.processor(
                text=prompts,
                images=batch_images,
                return_tensors="pt",
                truncation=False,
                padding="longest",
                max_length=self.args.max_token,
            ).to(self.accelerator.device)

            prompt_length = encoding.input_ids.size(1)
            max_new_tokens = self.args.max_token - prompt_length
            
            if max_new_tokens <= 0:
                self.accelerator.print(f"{max_new_tokens}:max_new_tokens<0")
                # Return empty predictions and attention scores if applicable
                empty_preds = [[""] * len(val_samples) for val_samples in batch_val_samples]
                if return_attention_scores:
                    return empty_preds, [[] for _ in batch_val_samples]
                return empty_preds

            # Single generate call to get both sequences and attentions
            outputs = self.model.generate(
                **encoding,
                max_new_tokens=max_new_tokens,
                temperature=0,
                do_sample=False,
                pad_token_id=self.processor.tokenizer.eos_token_id, # 151645
                output_attentions=return_attention_scores,
                return_dict_in_generate=True,
            )
            
        # Decode batch outputs
        # The generated sequences are the part of the output after the prompt
        generated_sequences = outputs.sequences[:, prompt_length:]
        generated_texts = self.processor.batch_decode(generated_sequences, skip_special_tokens=True)

        # Extract predictions for each batch
        batch_predictions = []
        for generated_text in generated_texts:
            # The generated text is already the response, no need to split by "assistant"
            responses_section = generated_text.strip()
            predictions = self.extract_predictions(responses_section)
            batch_predictions.append(predictions)

        if return_attention_scores:
            if self.args.attn_layer is not None:
                layer = self.args.attn_layer
            else:
                layer = -1 # Default to the last layer

            prompt_attentions = outputs.attentions[0] # corresponding to the first generated token

            # prompt_attentions is a tuple with length of num_layers
            # each element is a tensor with shape (batch_size, num_heads, seq_len, seq_len)
            
            # Select the specified layer and sum over the heads
            attn_score = torch.sum(prompt_attentions[layer], dim=1).to(dtype=torch.float16) # (batch_size, seq_len, seq_len)

            attn_layer = []
            IMAGE_TOKEN = "<|image_pad|>"
            for idx in range(len(prompts_comp)):  # batch_size_parallel
                inst, demo, response = prompts_comp[idx]
                images = batch_images[idx]
                inst_imgs_num = inst.count(IMAGE_TOKEN)
                demo_imgs_num = demo.count(IMAGE_TOKEN)
                response_imgs_num = response.count(IMAGE_TOKEN)

                if not inst and not demo and not response: # Skip failed samples
                    attn_layer.append([])
                    continue
                
                demo_list = batch_demo_list[idx]

                n_i_text = self.processor(
                    text=inst,
                    images=images[:inst_imgs_num],
                    return_tensors="pt",
                    truncation=False,
                    padding="longest",
                    max_length=self.args.max_token,
                ).to(self.accelerator.device)
                n_d_text = self.processor(
                    text=demo,
                    images=images[inst_imgs_num:inst_imgs_num+demo_imgs_num],
                    return_tensors="pt",
                    truncation=False,
                    padding="longest",
                    max_length=self.args.max_token,
                ).to(self.accelerator.device)
                n_r_text = self.processor(
                    text=response,
                    images=images[inst_imgs_num+demo_imgs_num:inst_imgs_num+demo_imgs_num+response_imgs_num],
                    return_tensors="pt",
                    truncation=False,
                    padding="longest",
                    max_length=self.args.max_token,
                ).to(self.accelerator.device)
                n_i = n_i_text.input_ids.size(1)
                n_d = n_d_text.input_ids.size(1)
                n_r = n_r_text.input_ids.size(1)

                # Recalculate demo_len for each demonstration, including image tokens
                demo_len = []
                image_ptr = inst_imgs_num
                for _demo_text, _ in demo_list:
                    image_cnt = _demo_text.count(IMAGE_TOKEN)
                    _demo_len = self.processor(
                        text=_demo_text,
                        images=images[image_ptr:image_ptr+image_cnt],
                        return_tensors="pt",
                        truncation=False,
                        padding="longest",
                        max_length=self.args.max_token,
                    ).to(self.accelerator.device)
                    image_ptr += image_cnt
                    demo_len.append(_demo_len.input_ids.size(1))

                # The total length used for slicing should be based on actual tokenized length
                # from the attention mask to be robust against right padding.
                total_prompt_len = encoding.attention_mask[idx].sum().item()
                
                start_of_demo_tokens = n_i
                end_of_demo_tokens = start_of_demo_tokens + n_d

                # Slice the attention matrix for the current example from the batch
                attn = attn_score[idx, :total_prompt_len, :total_prompt_len]

                response_start_token = end_of_demo_tokens
                response_end_token = response_start_token + n_r
                demo_to_response = attn[
                    response_start_token:response_end_token, start_of_demo_tokens:end_of_demo_tokens
                ]  

                demo_attn = []
                demo_idx = 0
                for i in range(len(demo_list)):
                    single_demo_to_response = demo_to_response[
                        :, demo_idx : demo_idx + demo_len[i]
                    ]

                    # Normalize by the area of the attention slice
                    norm_factor = (demo_len[i] * n_r) # divide by the rectangle area on the attention map (the Fig. 2 in the paper)
                    if norm_factor > 0:
                        demo_attn.append(single_demo_to_response.sum() / norm_factor)
                    else:
                        demo_attn.append(torch.tensor(0.0, device=attn.device))

                    demo_idx += demo_len[i]

                # Visualize attention maps for all layers with boundaries

                self.visualize_attention_maps_with_boundaries(
                    prompt_attentions, 
                    idx, 
                    prompts_comp[idx], 
                    batch_images[idx], 
                    n_i, n_d, n_r, 
                    demo_len, 
                    total_prompt_len,
                    encoding
                )
                
                attn_layer.append(demo_attn)

            return batch_predictions, attn_layer

        return batch_predictions

    def _prepare_model_inputs(self, demonstration_pairs, val_samples):
        prompt_template, instruction, val_inst, task_inst = DATASET_PROMPTS[f'{self.args.model_type}_{self.args.dataset}']

        # Prepare demonstrations
        demonstrations = []
        image_paths = []
        for demo, img_path in demonstration_pairs:
            demonstrations.append(demo)
            image_paths.append(img_path)

        # Prepare validation questions
        val_texts = []
        val_img_paths = []
        for i, sample in enumerate(val_samples):
            question, _, image = format_qwenvl_message_to_qa(sample)[0]
            val_texts.append(f'Question {i + 1}: {question.replace("Question: ","")}')
            val_img_paths.append(image)

        # Construct prompt
        inst, demo, response = (
            instruction,
            "\n".join(demonstrations),
            val_inst + "\n".join(val_texts) + task_inst,
        )
        prompt = inst + demo + response

        # Collect images
        all_image_paths = image_paths + val_img_paths

        try:
            IMAGE_BASE_DIR = "/obs/users/benhao/llava-en-zh-2k"
            images = [Image.open(os.path.join(IMAGE_BASE_DIR, p)).convert("RGB") for p in all_image_paths]
        except FileNotFoundError as e:
            self.accelerator.print(f"Error loading image: {e}")
            return None, None, None

        return prompt, images, (inst, demo, response)

    @torch.no_grad()
    def evaluate(
        self,
        dataset: List[Dict[str, Any]],
        val_set: Optional[List[Dict[str, Any]]] = None,
        use_kfold: bool = False,
    ) -> str:
        total_size = len(dataset)
        score = torch.zeros(
            total_size, dtype=torch.float16, device=self.accelerator.device
        )
        count = torch.zeros(total_size, dtype=torch.int32, device=score.device)

        if use_kfold:
            assert (
                val_set is None
            ), "Validation set should not be provided for k-fold evaluation"
            kf = KFold(n_splits=self.args.k_folds, shuffle=True, random_state=42)
            folds = list(kf.split(dataset))
            for fold_idx, (train_idx, val_idx) in enumerate(tqdm(folds, desc="K-Folds")):
                train_set = [dataset[i] for i in train_idx]
                val_set = [dataset[i] for i in val_idx]
                local_score = torch.zeros(
                    len(train_set), dtype=torch.float16, device=score.device
                )
                local_count = torch.zeros(
                    len(train_set), dtype=torch.int32, device=score.device
                )
                self._evaluate_single_fold(train_set, val_set, local_score, local_count)
                if not isinstance(train_idx, torch.Tensor):
                    train_idx = torch.tensor(
                        train_idx, dtype=torch.int32, device=score.device
                    )
                score.index_add_(0, train_idx, local_score)
                count.index_add_(0, train_idx, local_count)
                self.logger.info(
                    f"Fold {fold_idx + 1}/{self.args.k_folds} evaluation completed"
                )
        else:
            assert (
                val_set is not None
            ), "Validation set should be provided for single dataset evaluation"
            local_score = torch.zeros(
                len(dataset), dtype=torch.float16, device=score.device
            )
            local_count = torch.zeros(
                len(dataset), dtype=torch.int32, device=score.device
            )
            self._evaluate_single_fold(dataset, val_set, local_score, local_count)
            score.add_(local_score)
            count.add_(local_count)

        final_score = torch.where(
            count > 0, score / count, torch.zeros_like(score, dtype=torch.float16)
        )
        sorted_idx = torch.argsort(final_score, descending=True)
        sorted_dataset_with_scores = [
            {
                **dataset[i],
                "score": final_score[i].item(),
            }
            for i in sorted_idx.tolist()
        ]

        output_path = os.path.join(self.args.output_filtered_path, f"data_whisperer_qwen_vl.json")

        save_json(output_path, sorted_dataset_with_scores)
        self.logger.info(f"Fold evaluation completed. Results saved to {output_path}")
        return output_path

    def _evaluate_single_fold(
        self,
        train_set: List[Dict[str, Any]],
        val_set: List[Dict[str, Any]],
        score: torch.Tensor,
        count: torch.Tensor,
    ) -> None:
        train_size = len(train_set)
        val_size = len(val_set)

        train_set, val_set = self.accelerator.prepare(train_set, val_set)
        prompt_template, _, _, _ = DATASET_PROMPTS[f'{self.args.model_type}_{self.args.dataset}']

        # Generate training and validation batch indices
        train_batches = [
            (i, min(i + self.args.batch_train, train_size))
            for i in range(0, train_size, self.args.batch_train)
        ]
        val_batches = [
            (i, min(i + self.args.batch_test, val_size))
            for i in range(0, val_size, self.args.batch_test)
        ]

        train_pointer = 0
        val_pointer = 0
        fail = 0

        metric_function = METRICS[self.args.metric]
        
        progress_bar = tqdm(total=len(train_batches), desc="Evaluating Fold")
        while train_pointer < len(train_batches):
            batch_val_samples = []
            batch_selected_indices = []
            batch_demo_list = []
            
            batch_start_train_pointer = train_pointer
            # Prepare batch demonstrations and validation samples in parallel
            for _ in range(self.args.parallel_batches):
                if train_pointer >= len(train_batches):
                    break

                # Get train batch indices
                start_train_idx, end_train_idx = train_batches[train_pointer]
                selected_indices = list(range(start_train_idx, end_train_idx))
                batch_selected_indices.append(selected_indices)

                # Get validation batch indices
                start_test_idx, end_test_idx = val_batches[val_pointer]
                test_batch = val_set[start_test_idx:end_test_idx]

                # Generate demonstrations
                _, demo_list = self.generate_demonstrations(
                    train_set, selected_indices, prompt_template
                )
                batch_demo_list.append(demo_list)
                batch_val_samples.append(test_batch)
                # Update pointers
                train_pointer += 1
                val_pointer = (val_pointer + 1) % len(val_batches)

             # Generate predictions for the current batch
            batch_predictions, batch_attention_scores = self.predict_batch(
                batch_val_samples,
                batch_demo_list,
                return_attention_scores=True,
            )
            progress_bar.update(train_pointer - batch_start_train_pointer)

            # Update scores and counts efficiently on the GPU
            for predictions, val_samples, selected_indices, attention_scores in zip(
                batch_predictions,
                batch_val_samples,
                batch_selected_indices,
                batch_attention_scores,
            ):
                # We just pick first message as the reference
                def get_reference(val_sample):
                    for msg in val_sample['messages']:
                        if msg.get('role') == 'assistant':
                            return msg.get('content')
                    return None
                
                references = [get_reference(sample) for sample in val_samples]
                
                if not attention_scores: # Handle case where attention scores could not be computed
                    fail += 1
                    continue

                if not isinstance(attention_scores, torch.Tensor):
                    attention_scores = torch.tensor(
                        attention_scores, dtype=torch.float16, device=score.device
                    )

                weight = attention_scores / attention_scores.sum()
                
                if len(predictions) != len(references):
                    if len(predictions) > len(references):
                        predictions = predictions[: len(references)]
                    else:
                        fail += 1
                        continue

                for pred, ref in zip(predictions, references):
                    pred_score = metric_function(pred, ref)
                    if not isinstance(selected_indices, torch.Tensor):
                        # print('indices is not tensor')
                        selected_indices = torch.tensor(
                            selected_indices, dtype=torch.int64, device=score.device
                        )
                    if not isinstance(pred_score, torch.Tensor):
                        # print('scores is not tensor')
                        pred_score = torch.tensor(
                            [pred_score], dtype=torch.float16, device=score.device
                        ).expand(len(selected_indices))

                    weighted_scores = pred_score * weight

                    score.scatter_add_(0, selected_indices, weighted_scores)

                count[selected_indices] += len(references)
        
        progress_bar.close()
        self.logger.info(f"Failed batches: {fail}")
        for val_sample in val_set:
            prediction = self.predict_batch(train_set, val_sample)
            
            # Correctly extract reference from conversation history
            reference = None
            if val_sample.get('messages') and isinstance(val_sample['messages'], list):
                for msg in reversed(val_sample['messages']):
                    if msg.get('role') == 'assistant':
                        reference = msg.get('content')
                        break
            
            if reference is None:
                self.logger.warning("Could not find reference answer for validation sample.")
                continue

            pred_score = metric_function(prediction, reference)

            # Assign uniform scores to all training samples for this validation run
            score += pred_score
            count += 1
        
        self.logger.info(f"Evaluation for this fold completed.")

# Benhao: Some visualization codes should be moved to a separate file, as it's actually sharable for other prunners.

#### Visualization and Debugging ####
    def visualize_attention_maps_with_boundaries(
        self, 
        prompt_attentions, 
        batch_idx, 
        prompt_components, 
        images, 
        n_i, n_d, n_r, 
        demo_len, 
        total_prompt_len,
        encoding
    ):
        """
        Visualize attention maps for all layers with proper boundaries and image token positions.
        
        Args:
            prompt_attentions: Tuple of attention tensors for each layer
            batch_idx: Index of current batch item
            prompt_components: Tuple of (instruction, demonstration, response) texts
            images: List of images for this batch item
            n_i: Number of instruction tokens
            n_d: Number of demonstration tokens  
            n_r: Number of response tokens
            demo_len: List of lengths for each demonstration
            total_prompt_len: Total length of the prompt
            encoding: Tokenizer encoding output
        """
        if not hasattr(self.args, 'save_attention_visualizations') or not self.args.save_attention_visualizations:
            return
            
        inst, demo, response = prompt_components
        IMAGE_TOKEN = "<|image_pad|>"
        
        # Create output directory for attention visualizations with unique ID
        vis_dir = os.path.join(self.args.output_filtered_path, f"attention_visualizations_{self.unique_id}")
        os.makedirs(vis_dir, exist_ok=True)
        
        # Save text components to file
        self._save_text_components(inst, demo, response, vis_dir, batch_idx)
        
        self.logger.info(f"Saving attention visualizations to: {vis_dir}")
        
        # Calculate section boundaries
        boundaries = {
            'instruction': (0, n_i),
            'demonstration': (n_i, n_i + n_d),
            'response': (n_i + n_d, n_i + n_d + n_r)
        }
        
        # Calculate individual demo boundaries within demonstration section
        demo_boundaries = []
        demo_start = n_i
        for demo_length in demo_len:
            demo_boundaries.append((demo_start, demo_start + demo_length))
            demo_start += demo_length
        
        # Find image token positions
        image_positions = self._find_image_token_positions(encoding, batch_idx, IMAGE_TOKEN)
        
        # Visualize attention for each layer
        num_layers = len(prompt_attentions)
        for layer_idx in range(num_layers):
            layer_attention = prompt_attentions[layer_idx][batch_idx]  # Shape: (num_heads, seq_len, seq_len)
            
            # Average over attention heads
            avg_attention = torch.mean(layer_attention, dim=0)  # Shape: (seq_len, seq_len)
            
            # Slice to actual prompt length
            attention_matrix = avg_attention[:total_prompt_len, :total_prompt_len].detach().cpu().numpy()
            
            # Create visualization
            self._create_attention_visualization(
                attention_matrix,
                layer_idx,
                boundaries,
                demo_boundaries,
                image_positions,
                vis_dir,
                batch_idx,
                total_prompt_len
            )
            
        print(f"Attention visualizations saved to: {vis_dir}")
    
    def _save_text_components(self, inst, demo, response, vis_dir, batch_idx):
        """Save instruction, demonstration, and response text to a file."""
        text_file_path = os.path.join(vis_dir, f'text_components_batch_{batch_idx}.txt')
        
        with open(text_file_path, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write(f"TEXT COMPONENTS FOR BATCH {batch_idx}\n")
            f.write("=" * 80 + "\n\n")
            
            f.write("INSTRUCTION:\n")
            f.write("-" * 40 + "\n")
            f.write(inst + "\n\n")
            
            f.write("DEMONSTRATION:\n")
            f.write("-" * 40 + "\n")
            f.write(demo + "\n\n")
            
            f.write("RESPONSE:\n")
            f.write("-" * 40 + "\n")
            f.write(response + "\n\n")
            
            f.write("=" * 80 + "\n")
            f.write(f"STATISTICS:\n")
            f.write(f"Instruction length: {len(inst)} characters\n")
            f.write(f"Demonstration length: {len(demo)} characters\n")
            f.write(f"Response length: {len(response)} characters\n")
            f.write(f"Total length: {len(inst) + len(demo) + len(response)} characters\n")
        
        self.logger.debug(f"Text components saved to: {text_file_path}")

    def _find_image_token_positions(self, encoding, batch_idx, image_token):
        """Find positions of image tokens in the tokenized sequence."""
        # Get the tokenized input_ids for this batch item
        input_ids = encoding.input_ids[batch_idx]
        
        # Get the image token ID
        image_token_id = self.tokenizer.convert_tokens_to_ids(image_token)
        
        # Find all positions where image tokens appear
        image_positions = []
        for pos, token_id in enumerate(input_ids):
            if token_id == image_token_id:
                image_positions.append(pos.item() if torch.is_tensor(pos) else pos)
        
        return image_positions
    
    def _add_section_braces(self, ax, boundaries, seq_len):
        """Add braces to indicate section boundaries on X and Y axes."""
        colors = {'instruction': 'red', 'demonstration': 'blue', 'response': 'green'}
        
        # Get axis limits
        ylim = ax.get_ylim()
        xlim = ax.get_xlim()
        
        # Add braces on X-axis (bottom)
        brace_offset_x = seq_len * 0.08  # Offset from the main plot
        for section, (start, end) in boundaries.items():
            if start < seq_len:
                end = min(end, seq_len)
                mid_pos = (start + end) / 2
                
                # Draw brace
                self._draw_brace(ax, start, end, ylim[0] + brace_offset_x, 'horizontal', colors[section])
                
                # Add label
                ax.text(mid_pos, ylim[0] + brace_offset_x * 1.5, section.capitalize(), 
                       ha='center', va='bottom', color=colors[section], 
                       fontsize=12, fontweight='bold')
        
        # Add braces on Y-axis (left)
        brace_offset_y = seq_len * 0.08  # Offset from the main plot
        for section, (start, end) in boundaries.items():
            if start < seq_len:
                end = min(end, seq_len)
                mid_pos = (start + end) / 2
                
                # Draw brace
                self._draw_brace(ax, start, end, xlim[0] - brace_offset_y, 'vertical', colors[section])
                
                # Add label
                ax.text(xlim[0] - brace_offset_y * 1.5, mid_pos, section.capitalize(), 
                       ha='right', va='center', color=colors[section], 
                       fontsize=12, fontweight='bold', rotation=90)
    
    def _draw_brace(self, ax, start, end, offset, orientation, color):
        """Draw a brace to indicate a section."""
        if orientation == 'horizontal':
            # Draw horizontal brace (for X-axis)
            # Main horizontal line
            ax.plot([start, end], [offset, offset], color=color, linewidth=2)
            # Start vertical line
            ax.plot([start, start], [offset - 1, offset + 1], color=color, linewidth=2)
            # End vertical line  
            ax.plot([end, end], [offset - 1, offset + 1], color=color, linewidth=2)
        else:  # vertical
            # Draw vertical brace (for Y-axis)
            # Main vertical line
            ax.plot([offset, offset], [start, end], color=color, linewidth=2)
            # Start horizontal line
            ax.plot([offset - 1, offset + 1], [start, start], color=color, linewidth=2)
            # End horizontal line
            ax.plot([offset - 1, offset + 1], [end, end], color=color, linewidth=2)
    
    def _create_attention_visualization(
        self, 
        attention_matrix, 
        layer_idx, 
        boundaries, 
        demo_boundaries, 
        image_positions, 
        save_dir, 
        batch_idx, 
        seq_len
    ):
        """
        Create a comprehensive attention visualization with boundaries and annotations.
        """
        fig, ax = plt.subplots(1, 1, figsize=(25, 25))  # Single large square plot
        
        # Use standard attention weights (no log transformation)
        attention_matrix_vis = attention_matrix.copy()
        
        # Apply min-max normalization for better visualization (standard practice)
        attention_min = np.percentile(attention_matrix_vis, 5)  # Use 5th percentile to avoid extreme outliers
        attention_max = np.percentile(attention_matrix_vis, 95)  # Use 95th percentile
        attention_matrix_vis = np.clip(attention_matrix_vis, attention_min, attention_max)
        attention_matrix_vis = (attention_matrix_vis - attention_min) / (attention_max - attention_min)
        
        # Main attention heatmap with standard normalization
        im = ax.imshow(attention_matrix_vis, cmap='viridis', aspect='equal', origin='upper', 
                        vmin=0, vmax=1)
        ax.set_title(f'Layer {layer_idx} Attention Matrix\n(Batch {batch_idx}, Seq Length: {seq_len})', fontsize=20)
        ax.set_xlabel('Key Position', fontsize=16)
        ax.set_ylabel('Query Position', fontsize=16)
        
        # Add colorbar for attention magnitude
        cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label('Attention Score (Normalized)', rotation=270, labelpad=15, fontsize=14)
        
        # Add main section separation lines
        colors = {'instruction': 'red', 'demonstration': 'orange', 'response': 'white'}
        for section, (start, end) in boundaries.items():
            if start < seq_len and start > 0:
                # Vertical lines (key boundaries)
                ax.axvline(x=start, color=colors[section], linestyle='-', linewidth=4, alpha=0.9)
                # Horizontal lines (query boundaries)  
                ax.axhline(y=start, color=colors[section], linestyle='-', linewidth=4, alpha=0.9)
            if end < seq_len:
                ax.axvline(x=end, color=colors[section], linestyle='-', linewidth=4, alpha=0.9)
                ax.axhline(y=end, color=colors[section], linestyle='-', linewidth=4, alpha=0.9)
        
        # Add individual demo boundaries within demonstration section
        demo_start = boundaries['demonstration'][0]
        current_pos = demo_start
        for i, (d_start, d_end) in enumerate(demo_boundaries):
            if i > 0 and d_start < seq_len:  # Don't draw line at the very beginning
                # Vertical and horizontal lines to separate each demo
                ax.axvline(x=d_start, color='yellow', linestyle='--', linewidth=2, alpha=0.8)
                ax.axhline(y=d_start, color='yellow', linestyle='--', linewidth=2, alpha=0.8)
        
        # Add response section subdivisions (if there are multiple responses)
        response_start, response_end = boundaries['response']
        # For now, we'll just mark the response section clearly
        # If you have multiple response parts, we can add more subdivisions here
        
        # Add attention values on the heatmap with improved visibility
        if seq_len <= 100:  # Increase the threshold since we have more space now
            for i in tqdm(range(min(seq_len, attention_matrix.shape[0])), total=min(seq_len, attention_matrix.shape[0]), desc="Adding attention values", leave=False):
                for j in range(min(seq_len, attention_matrix.shape[1])):
                    value = attention_matrix[i, j]
                    # Only show values above a certain threshold to avoid clutter
                    if value > 0.001:  # Only show meaningful attention values
                        # Format to 2 significant digits
                        if value >= 0.01:
                            text = f'{value:.2f}'
                        elif value >= 0.001:
                            text = f'{value:.3f}'
                        else:
                            text = f'{value:.1e}'
                        
                        # Use white text for better contrast on viridis colormap
                        text_color = 'white' if attention_matrix_vis[i, j] > 0.5 else 'red'
                        ax.text(j, i, text, ha='center', va='center', 
                                color=text_color, fontsize=12, fontweight='bold')
        
        # Add section braces on axes
        self._add_section_braces(ax, boundaries, seq_len)
        
        # Add demo labels on the side
        demo_start = boundaries['demonstration'][0]
        for i, (d_start, d_end) in enumerate(demo_boundaries):
            if d_start < seq_len:
                mid_pos = (d_start + min(d_end, seq_len)) / 2
                # Add demo label on the right side
                ax.text(seq_len + seq_len * 0.02, mid_pos, f'Demo {i+1}', 
                       ha='left', va='center', color='yellow', fontsize=14, fontweight='bold',
                       bbox=dict(boxstyle="round,pad=0.3", facecolor='black', alpha=0.7))
        
        plt.tight_layout()

        self.logger.debug(f"Saving attention visualization to: {save_dir}")
        
        # Save the figure as PDF only (vector format)
        save_path_pdf = os.path.join(save_dir, f'attention_layer_{layer_idx}_batch_{batch_idx}.pdf')
        
        plt.savefig(save_path_pdf, format='pdf', bbox_inches='tight', facecolor='white')
        plt.close()

        self.logger.debug(f"Attention layer {layer_idx} saved to: {save_path_pdf}")
        # Create a summary statistics plot
        self._create_attention_statistics_plot(attention_matrix, layer_idx, boundaries, demo_boundaries, save_dir, batch_idx)
        self.logger.debug("Attention statistics plot created")
    
    def _create_attention_statistics_plot(self, attention_matrix, layer_idx, boundaries, demo_boundaries, save_dir, batch_idx):
        """Create statistical analysis plots for attention patterns."""
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(20, 16))
        
        # 1. Block-wise Attention Map (comprehensive section interactions)
        self._create_blockwise_attention_map(attention_matrix, boundaries, demo_boundaries, ax1, layer_idx)
        
        # Store the section patterns for debugging
        section_patterns = self._calculate_section_patterns(attention_matrix, boundaries)
        
        # 2. Response attention to individual demonstrations
        response_start, response_end = boundaries['response']
        demo_start, demo_end = boundaries['demonstration']
        
        if response_start < attention_matrix.shape[0] and demo_start < attention_matrix.shape[1]:
            demo_attention_scores = []
            demo_labels = []
            
            self.logger.debug(f"Bar Plot Response→Demo Calculation (Raw Attention Scores):")
            for i, (d_start, d_end) in enumerate(demo_boundaries):
                if d_end <= attention_matrix.shape[1]:
                    response_slice = attention_matrix[response_start:min(response_end, attention_matrix.shape[0]), d_start:d_end]
                    if response_slice.size > 0:
                        raw_score = response_slice.mean()
                        demo_attention_scores.append(raw_score)
                        demo_labels.append(f'Demo {i+1}')
                        self.logger.debug(f"    Demo {i+1}: {raw_score:.6f} (slice shape: {response_slice.shape})")
            
            if demo_attention_scores:
                ax2.bar(demo_labels, demo_attention_scores, color='cyan', alpha=0.7)
                ax2.set_title(f'Layer {layer_idx}: Response Attention to Each Demo\n(Raw Attention Scores)')
                ax2.set_ylabel('Raw Average Attention Score')
                ax2.tick_params(axis='x', rotation=45)
                
                # Add value labels on bars for better visibility
                for i, (label, score) in enumerate(zip(demo_labels, demo_attention_scores)):
                    ax2.text(i, score + max(demo_attention_scores) * 0.01, 
                            f'{score:.4f}', ha='center', va='bottom', fontweight='bold')
        
        # 3. Attention entropy across sequence positions (corrected calculation)
        attention_entropy = []
        max_entropy = np.log(attention_matrix.shape[1])  # Maximum possible entropy
        
        for i in range(attention_matrix.shape[0]):
            row = attention_matrix[i, :]
            
            # Normalize the row to ensure it sums to 1 (like a probability distribution)
            row_sum = row.sum()
            if row_sum > 0:
                row = row / row_sum
            else:
                row = np.ones_like(row) / len(row)  # Uniform distribution if all zeros
            
            # Add small epsilon to avoid log(0)
            row = row + 1e-10
            row = row / row.sum()  # Re-normalize after adding epsilon
            
            # Calculate entropy: H = -Σ(p_i * log(p_i))
            entropy = -np.sum(row * np.log(row))
            attention_entropy.append(entropy)
        
        ax3.plot(attention_entropy, color='purple', linewidth=2, label='Attention Entropy')
        ax3.axhline(y=max_entropy, color='gray', linestyle=':', alpha=0.7, 
                   label=f'Max Entropy ({max_entropy:.2f})')
        ax3.set_title(f'Layer {layer_idx}: Attention Entropy by Position\n'
                     f'(Low=Focused, High=Distributed)')
        ax3.set_xlabel('Sequence Position (Query)')
        ax3.set_ylabel('Attention Entropy (nats)')
        ax3.grid(True, alpha=0.3)
        ax3.legend()
        
        # Add section boundaries to entropy plot
        colors = {'instruction': 'red', 'demonstration': 'blue', 'response': 'green'}
        for section, (start, end) in boundaries.items():
            ax3.axvline(x=start, color=colors[section], linestyle='--', alpha=0.7, label=f'{section.capitalize()}')
        ax3.legend()
        
        # 4. Attention magnitude distribution with statistics
        ax4.hist(attention_matrix.flatten(), bins=50, alpha=0.7, color='orange', edgecolor='black')
        ax4.set_title(f'Layer {layer_idx}: Attention Score Distribution')
        ax4.set_xlabel('Attention Score')
        ax4.set_ylabel('Frequency')
        
        # Add statistical lines
        mean_val = attention_matrix.mean()
        median_val = np.median(attention_matrix)
        max_val = attention_matrix.max()
        min_val = attention_matrix.min()
        
        ax4.axvline(x=mean_val, color='red', linestyle='--', linewidth=2, 
                   label=f'Mean: {mean_val:.4f}')
        ax4.axvline(x=median_val, color='blue', linestyle='--', linewidth=2, 
                   label=f'Median: {median_val:.4f}')
        ax4.axvline(x=max_val, color='green', linestyle='--', linewidth=2, 
                   label=f'Max: {max_val:.4f}')
        ax4.legend()
        
        plt.tight_layout()
        
        # Print comprehensive debugging information
        self.logger.debug(f"\n=== Layer {layer_idx} Block-wise Attention Analysis ===")
        self.logger.debug(f"Attention matrix shape: {attention_matrix.shape}")
        self.logger.debug(f"Attention range: [{min_val:.6f}, {max_val:.6f}]")
        self.logger.debug(f"Mean attention: {mean_val:.6f}, Median: {median_val:.6f}")
        
        # Print section-level patterns
        self.logger.debug(f"\nSection-Level Patterns:")
        for pattern_type, values in section_patterns.items():
            if values:
                self.logger.debug(f"  {pattern_type}:")
                for section, score in values.items():
                    self.logger.debug(f"    {section}: {score:.6f}")
        
        # Print attention entropy analysis
        self.logger.debug(f"\nAttention Entropy Analysis:")
        self.logger.debug(f"  Average entropy: {np.mean(attention_entropy):.4f}")
        self.logger.debug(f"  Max possible entropy: {max_entropy:.4f}")
        self.logger.debug(f"  Entropy efficiency: {np.mean(attention_entropy)/max_entropy:.3f} (0=focused, 1=uniform)")
        
        # Find and report key attention patterns from block-wise map
        self.logger.debug(f"\nKey Block-wise Attention Patterns:")
        
        # Analyze the block-wise attention patterns
        try:
            # Get the block attention data for analysis
            block_attention_data = self._get_block_attention_for_analysis(attention_matrix, boundaries, demo_boundaries)
            if block_attention_data is not None:
                self._analyze_block_patterns(block_attention_data, boundaries, demo_boundaries, layer_idx)
        except Exception as e:
            self.logger.debug(f"  Could not analyze block patterns: {e}")
        
        self.logger.debug(f"  → See block-wise attention heatmap for visual analysis")
        self.logger.debug("="*60)
        
        # Save the statistics plot as PDF only (vector format)
        save_path_pdf = os.path.join(save_dir, f'attention_stats_layer_{layer_idx}_batch_{batch_idx}.pdf')
        
        plt.savefig(save_path_pdf, format='pdf', bbox_inches='tight', facecolor='white')
        plt.close()
        
        self.logger.debug(f"Attention statistics saved to: {save_path_pdf}")
    
    def _create_blockwise_attention_map(self, attention_matrix, boundaries, demo_boundaries, ax, layer_idx):
        """
        Create a detailed block-wise attention map showing all section interactions.
        
        This visualization provides a comprehensive view of how different parts of the input
        attend to each other, going beyond simple section-level analysis.
        
        Features:
        - Instruction block: Shows how instruction tokens interact
        - Individual Demo blocks: Each demonstration is shown separately  
        - Response parts: Long responses are subdivided for finer analysis
        - Color-coded section indicators: Red=Instruction, Blue=Demo, Green=Response
        - Diagonal elements: Self-attention within each block
        - Off-diagonal elements: Cross-attention between different blocks
        
        Interpretation:
        - High values on diagonal: Strong self-attention within sections
        - High Response→Demo values: Response attending to demonstrations
        - High Demo→Instruction values: Demos attending to instructions
        - Cross-demo attention: How different demos interact with each other
        
        Args:
            attention_matrix: The attention matrix to analyze [seq_len, seq_len]
            boundaries: Section boundaries dict (instruction, demonstration, response)
            demo_boundaries: List of individual demo boundaries
            ax: Matplotlib axis to plot on
            layer_idx: Current layer index for labeling
        """
        
        # Create extended boundaries including individual demos and response parts
        extended_boundaries = []
        extended_labels = []
        
        # Add instruction
        inst_start, inst_end = boundaries['instruction']
        if inst_start < attention_matrix.shape[0]:
            extended_boundaries.append((inst_start, min(inst_end, attention_matrix.shape[0])))
            extended_labels.append('Instruction')
        
        # Add individual demos
        demo_start, demo_end = boundaries['demonstration']
        for i, (d_start, d_end) in enumerate(demo_boundaries):
            if d_start < attention_matrix.shape[0]:
                extended_boundaries.append((d_start, min(d_end, attention_matrix.shape[0])))
                extended_labels.append(f'Demo {i+1}')
        
        # Add response as a single block
        resp_start, resp_end = boundaries['response']
        if resp_start < attention_matrix.shape[0]:
            extended_boundaries.append((resp_start, min(resp_end, attention_matrix.shape[0])))
            extended_labels.append('Response')
        
        # Create block-wise attention matrix
        n_blocks = len(extended_boundaries)
        block_attention = np.zeros((n_blocks, n_blocks))
        
        self.logger.debug(f"  Block-wise Attention Map Calculation (Raw Attention Scores):")
        self.logger.debug(f"    Extended boundaries: {[(label, start, end) for label, (start, end) in zip(extended_labels, extended_boundaries)]}")
        
        for i, (query_start, query_end) in enumerate(extended_boundaries):
            for j, (key_start, key_end) in enumerate(extended_boundaries):
                if (query_end <= attention_matrix.shape[0] and 
                    key_end <= attention_matrix.shape[1] and
                    query_start < query_end and key_start < key_end):
                    
                    # Extract the block and calculate mean attention
                    block = attention_matrix[query_start:query_end, key_start:key_end]
                    if block.size > 0:
                        raw_score = block.mean()
                        block_attention[i, j] = raw_score
                        
                        # Debug Response→Demo specifically 
                        if 'Response' in extended_labels[i] and 'Demo' in extended_labels[j]:
                            self.logger.debug(f"    {extended_labels[i]} → {extended_labels[j]}: {raw_score:.6f} (block shape: {block.shape})")
        
        # Store raw block attention for later comparison
        raw_block_attention = block_attention.copy()
        
        # Normalize the block attention for better visualization
        if block_attention.max() > 0:
            # Use log scale for better contrast if values span large range
            min_val = block_attention[block_attention > 0].min() if np.any(block_attention > 0) else 0
            max_val = block_attention.max()
            if max_val / min_val > 10:  # If range spans more than 10x
                # Use log scale with small offset to avoid log(0)
                block_attention_vis = np.log(block_attention + min_val * 0.01)
                vmin, vmax = np.log(min_val * 0.01), np.log(max_val + min_val * 0.01)
            else:
                block_attention_vis = block_attention
                vmin, vmax = 0, max_val
        else:
            block_attention_vis = block_attention
            vmin, vmax = 0, 1
        
        # Create heatmap with better colormap
        im = ax.imshow(block_attention_vis, cmap='YlOrRd', aspect='equal', vmin=vmin, vmax=vmax)
        
        # Customize the plot
        ax.set_title(f'Layer {layer_idx}: Block-wise Attention Map\n(Rows=Query, Cols=Key, Diagonal=Self-Attention)', 
                    fontsize=13, fontweight='bold')
        ax.set_xlabel('Key Sections (What is being attended to)', fontsize=11)
        ax.set_ylabel('Query Sections (What is attending)', fontsize=11)
        
        # Set tick labels
        ax.set_xticks(range(n_blocks))
        ax.set_yticks(range(n_blocks))
        ax.set_xticklabels(extended_labels, rotation=45, ha='right', fontsize=10)
        ax.set_yticklabels(extended_labels, fontsize=10)
        
        # Add colorbar
        cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label('Average Attention Score', rotation=270, labelpad=15, fontsize=10)
        
        # Add text annotations with attention values (RAW scores, not normalized)
        for i in range(n_blocks):
            for j in range(n_blocks):
                # Use RAW block_attention values, NOT the normalized block_attention_vis
                raw_value = raw_block_attention[i, j]  # Use the stored raw values
                if raw_value > 0:  # Only show non-zero values
                    # Use normalized value ONLY for determining text color, not for display
                    normalized_value = (block_attention_vis[i, j] - vmin) / (vmax - vmin)
                    text_color = 'white' if normalized_value > 0.5 else 'black'
                    
                    # Format value display - ALWAYS show RAW values
                    if raw_value >= 0.001:
                        text = f'{raw_value:.3f}'
                    else:
                        text = f'{raw_value:.1e}'
                    
                    ax.text(j, i, text, ha='center', va='center', 
                           color=text_color, fontsize=9, fontweight='bold')
                    
                    # Debug Response→Demo text annotations specifically
                    if 'Response' in extended_labels[i] and 'Demo' in extended_labels[j]:
                        self.logger.debug(f"    Text annotation {extended_labels[i]} → {extended_labels[j]}: displaying {text} (raw: {raw_value:.6f})")
        
        # Add grid for better visualization
        ax.set_xticks(np.arange(-0.5, n_blocks, 1), minor=True)
        ax.set_yticks(np.arange(-0.5, n_blocks, 1), minor=True)
        ax.grid(which='minor', color='white', linestyle='-', linewidth=1, alpha=0.8)
        
        # Add section type indicators with color coding
        section_colors = {'Instruction': 'red', 'Demo': 'blue', 'Response': 'green'}
        for i, label in enumerate(extended_labels):
            for section_type, color in section_colors.items():
                if section_type in label:
                    # Add colored indicators on the axes
                    rect_x = plt.Rectangle((i-0.5, -0.5), 1, 0.3, facecolor=color, alpha=0.7)
                    rect_y = plt.Rectangle((-0.5, i-0.5), 0.3, 1, facecolor=color, alpha=0.7)
                    ax.add_patch(rect_x)
                    ax.add_patch(rect_y)
                    break
    
    def _calculate_section_patterns(self, attention_matrix, boundaries):
        """Calculate various attention patterns for debugging."""
        section_patterns = {
            'Self-Attention': {},      # Within-section attention
            'Receiving Attention': {}, # How much attention each section receives from others
            'Giving Attention': {}     # How much attention each section gives to others
        }
        
        for section, (start, end) in boundaries.items():
            end = min(end, attention_matrix.shape[0])
            if start < attention_matrix.shape[0]:
                # Self-attention within section (diagonal blocks)
                if end <= attention_matrix.shape[1]:
                    self_attn = attention_matrix[start:end, start:end].mean()
                    section_patterns['Self-Attention'][section] = self_attn
                
                # How much attention this section receives (as keys)
                if end <= attention_matrix.shape[1]:
                    received_attn = attention_matrix[:, start:end].mean()
                    section_patterns['Receiving Attention'][section] = received_attn
                
                # How much attention this section gives (as queries)
                given_attn = attention_matrix[start:end, :].mean()
                section_patterns['Giving Attention'][section] = given_attn
        
        return section_patterns
    
    def _get_block_attention_for_analysis(self, attention_matrix, boundaries, demo_boundaries):
        """Extract block attention matrix for detailed analysis."""
        try:
            # Recreate the same logic as in _create_blockwise_attention_map
            extended_boundaries = []
            extended_labels = []
            
            # Add instruction
            inst_start, inst_end = boundaries['instruction']
            if inst_start < attention_matrix.shape[0]:
                extended_boundaries.append((inst_start, min(inst_end, attention_matrix.shape[0])))
                extended_labels.append('Instruction')
            
            # Add individual demos
            for i, (d_start, d_end) in enumerate(demo_boundaries):
                if d_start < attention_matrix.shape[0]:
                    extended_boundaries.append((d_start, min(d_end, attention_matrix.shape[0])))
                    extended_labels.append(f'Demo {i+1}')
            
            # Add response as a single block
            resp_start, resp_end = boundaries['response']
            if resp_start < attention_matrix.shape[0]:
                extended_boundaries.append((resp_start, min(resp_end, attention_matrix.shape[0])))
                extended_labels.append('Response')
            
            # Create block-wise attention matrix
            n_blocks = len(extended_boundaries)
            block_attention = np.zeros((n_blocks, n_blocks))
            
            for i, (query_start, query_end) in enumerate(extended_boundaries):
                for j, (key_start, key_end) in enumerate(extended_boundaries):
                    if (query_end <= attention_matrix.shape[0] and 
                        key_end <= attention_matrix.shape[1] and
                        query_start < query_end and key_start < key_end):
                        
                        block = attention_matrix[query_start:query_end, key_start:key_end]
                        if block.size > 0:
                            block_attention[i, j] = block.mean()
            
            return block_attention, extended_labels
        except Exception as e:
            self.logger.debug(f"Error creating block attention for analysis: {e}")
            return None, None
    
    def _analyze_block_patterns(self, block_attention_tuple, boundaries, demo_boundaries, layer_idx):
        """Analyze and report key patterns in the block attention matrix (RAW scores)."""
        if block_attention_tuple is None:
            return
            
        block_attention, extended_labels = block_attention_tuple
        if block_attention is None or len(extended_labels) == 0:
            return
        
        n_blocks = len(extended_labels)
        
        self.logger.debug(f"  Block Pattern Analysis (RAW Attention Scores):")
        
        # Find strongest self-attention blocks
        self_attention_scores = []
        for i in range(n_blocks):
            self_attention_scores.append((block_attention[i, i], extended_labels[i]))
        self_attention_scores.sort(reverse=True, key=lambda x: x[0])
        
        self.logger.debug(f"  Strongest Self-Attention (RAW scores):")
        for score, label in self_attention_scores[:3]:  # Top 3
            if score > 0:
                self.logger.debug(f"    {label}: {score:.6f}")
        
        # Find strongest cross-attention patterns
        cross_attention = []
        for i in range(n_blocks):
            for j in range(n_blocks):
                if i != j and block_attention[i, j] > 0:  # Skip diagonal
                    cross_attention.append((block_attention[i, j], f"{extended_labels[i]} → {extended_labels[j]}"))
        
        if cross_attention:
            cross_attention.sort(reverse=True, key=lambda x: x[0])
            self.logger.debug(f"  Strongest Cross-Attention (RAW scores):")
            for score, pattern in cross_attention[:5]:  # Top 5
                self.logger.debug(f"    {pattern}: {score:.6f}")
        
        # Analyze response attention patterns specifically
        response_blocks = [i for i, label in enumerate(extended_labels) if 'Response' in label]
        demo_blocks = [i for i, label in enumerate(extended_labels) if 'Demo' in label]
        
        if response_blocks and demo_blocks:
            self.logger.debug(f"  Response → Demo Attention (RAW scores):")
            for resp_idx in response_blocks:
                resp_label = extended_labels[resp_idx]
                demo_attentions = []
                for demo_idx in demo_blocks:
                    demo_label = extended_labels[demo_idx]
                    raw_score = block_attention[resp_idx, demo_idx]
                    if raw_score > 0:
                        demo_attentions.append((raw_score, demo_label))
                        self.logger.debug(f"    {resp_label} → {demo_label}: {raw_score:.6f}")
                
                if demo_attentions:
                    demo_attentions.sort(reverse=True, key=lambda x: x[0])
                    best_score, best_demo = demo_attentions[0]
                    self.logger.debug(f"    → {resp_label} most attends to {best_demo}: {best_score:.6f}")
        
        # Calculate and report attention distribution metrics
        total_attention = block_attention.sum()
        if total_attention > 0:
            diagonal_sum = np.trace(block_attention)
            off_diagonal_sum = total_attention - diagonal_sum
            
            self.logger.debug(f"  Attention Distribution (RAW scores):")
            self.logger.debug(f"    Self-attention (diagonal): {diagonal_sum/total_attention:.3f}")
            self.logger.debug(f"    Cross-attention (off-diagonal): {off_diagonal_sum/total_attention:.3f}")
#### End of Visualization and Debugging ####
