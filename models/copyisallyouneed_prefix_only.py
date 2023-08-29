from header import *
import sys, random
import numpy as np
from time import time
from tqdm import tqdm
import collections
from .util_func import *
import faiss
import faiss.contrib.torch_utils

# torch.set_printoptions(profile='full')

class CopyisallyouneedPrefixOnly(nn.Module):

    def __init__(self, **args):
        super(CopyisallyouneedPrefixOnly, self).__init__()
        self.args = args
        if args['random_initialize']:
            if args['local_rank'] == 0:
                print('[!] model random initialized.')
            # GPT model
            GPT_config = AutoConfig.from_pretrained(
                self.args['prefix_encoder_model'][self.args['lang']]
            )
            self.model = GPT2LMHeadModel(GPT_config)
            # self.model = AutoModel.from_config(GPT_config)
        else:
            self.model = GPT2LMHeadModel.from_pretrained(self.args['prefix_encoder_model'][self.args['lang']])
        # GPT tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(self.args['prefix_encoder_tokenizer'][self.args['lang']])

        self.vocab_size = len(self.tokenizer)
        self.pad = self.tokenizer.pad_token_id if self.args['lang'] == 'zh' else self.tokenizer.eos_token_id
        self.token_embeddings = nn.Parameter(list(self.model.lm_head.parameters())[0])
        assert self.token_embeddings.shape[0] == self.vocab_size
        
        self.phrase_dim = self.args['phrase_dim'] if 'phrase_dim' in self.args else self.model.config.hidden_size
        if self.phrase_dim != self.model.config.hidden_size:
            self.dim_proj = nn.Linear(self.model.config.hidden_size, self.phrase_dim)
        else:
            self.dim_proj = None
        self.gen_loss_fct = nn.CrossEntropyLoss(ignore_index=self.pad)

    @torch.no_grad()
    def get_query_rep(self, ids):
        self.eval()
        output = self.model(input_ids=ids, output_hidden_states=True)['hidden_states'][-1][:, -1, :]
        return output

    def get_token_loss(self, ids, hs, AR_mask, token_reps, do_eval=True):
        label = ids[:, 1:].reshape(-1)
        label_mask = AR_mask[:, 1:].reshape(-1)
        label = label[label_mask]
        logits = torch.matmul(
            hs[:, :-1, :],
            token_reps.t()
        )
        logits = logits.reshape(-1, logits.shape[-1])
        logits = logits[label_mask]

        # logits /= self.args['temp']
        loss = self.gen_loss_fct(logits, label)
        if not do_eval:
            return loss
        else:
            chosen_tokens = torch.max(logits, dim=-1)[1]
            gen_acc = (chosen_tokens.reshape(-1) == label.reshape(-1)).to(torch.long)
            gen_acc = gen_acc.sum().item() / len(gen_acc)
            return loss, gen_acc

    def forward(self, batch):  
        model_st_time = time()
        token_reps = self.token_embeddings
        if self.dim_proj is not None:
            token_reps = self.dim_proj(token_reps)
        
        ## gpt2 query encoder
        ids, ids_mask = batch['gpt2_ids'].cuda(), batch['gpt2_mask'].cuda()
        query_hidden_states = \
        self.model(input_ids=ids, attention_mask=ids_mask, output_hidden_states=True).hidden_states[-1]
        if self.dim_proj is not None:
            query_hidden_states = self.dim_proj(query_hidden_states)
        
        AR_mask = batch['AR_mask'].to(torch.bool).cuda()
        AR_loss = self.get_token_loss(ids, query_hidden_states, AR_mask, token_reps, do_eval=False)

        phrase_rep = batch['phrase_embedding']
        if phrase_rep is None:
            return AR_loss, {}
        
        phrase_rep = phrase_rep.cuda()
        candidate_reps = torch.cat([
            token_reps,
            phrase_rep], dim=0)
    
        false_neg_mask = batch['false_neg_mask'].cuda()
        false_neg_mask = false_neg_mask.reshape(-1, false_neg_mask.shape[-1])
        labels = batch['phrase_label'].cuda()

        query = query_hidden_states[:, :-1].reshape(-1, query_hidden_states.shape[-1])
        query_mask = AR_mask[:, 1:].reshape(-1).to(torch.bool)
        query = query[query_mask]
        labels = labels[:, :-1].reshape(-1)
        labels = labels[query_mask]
        phrase_indexes = labels >= self.vocab_size
        # token_indexes = ~phrase_indexes
        # phrase_num = phrase_indexes.sum()
        # token_num = token_indexes.sum()

        logits = torch.matmul(query, candidate_reps.t())
        logits /= self.args['temp']
        full_false_neg_mask = torch.ones_like(logits).to(torch.long)
        full_false_neg_mask[phrase_indexes] = false_neg_mask

        # print(false_neg_mask.shape, logits.shape)
        logits = torch.where(full_false_neg_mask.to(torch.bool), logits, torch.tensor(-np.inf).to(torch.half).cuda())
        if torch.isnan(logits).any():
            sys.stderr.write(f"logits nan\n")
            sys.stderr.write(f"logits: {logits}\n")
            sys.stderr.write(f"query: {self.tokenizer.decode(ids.reshape(-1))}\n")
            sys.stderr.write(f"doc: {self.bert_tokenizer.decode(dids.reshape(-1))}\n")
            sys.stderr.write(f"labels: {labels}\n")
            exit()
        # ## split the token accuaracy and phrase accuracy
        # phrase_acc = logits[phrase_indexes].max(dim=-1)[1] == labels[phrase_indexes]
        # phrase_acc = phrase_acc.to(torch.float).mean().item()
        # if token_num > 0: # no token
        #     token_acc = logits[token_indexes].max(dim=-1)[1] == labels[token_indexes]
        #     token_acc = token_acc.to(torch.float).mean().item()
        # else:
        #     token_acc = -1
        if self.args['loss_type'] == 'focal_loss':
            logit_logsoftmax = F.log_softmax(logits, dim=-1)
            logit_softmax = torch.exp(logit_logsoftmax)
            p = logit_softmax[range(len(logits)), labels]
            logp = logit_logsoftmax[range(len(logits)), labels]
            alpha = 0.25
            gamma = 2
            loss_ = -alpha * (1 - p) ** gamma * logp
            loss = loss_.mean()
            # beta = self.args['beta']
            # loss = 0
            # if phrase_num > 0:
            #     loss += beta * loss_[phrase_indexes].mean()
            # if token_num > 0:
            #     loss += (1 - beta) * loss_[token_indexes].mean()
        else:
            loss = F.log_softmax(logits, dim=-1)
            loss = loss[range(len(logits)), labels]
            loss = (-loss.sum(dim=-1)).mean()
        if torch.isnan(loss).any():
            sys.stderr.write(f"loss nan\n")
            sys.stderr.write(f"loss: {loss}\n")
            sys.stderr.write(f"logits: {logits}\n")
            sys.stderr.write(f"query: {self.tokenizer.decode(ids.reshape(-1))}\n")
            sys.stderr.write(f"doc: {self.bert_tokenizer.decode(dids.reshape(-1))}\n")
            sys.stderr.write(f"labels: {labels}\n")
            exit()
        result_dict = {}
        # result_dict = {
        #     'phrase_acc': phrase_acc,
        #     'token_acc': token_acc
        # }
        model_end_time = time()
        print("model time:", model_end_time - model_st_time)
        return (
            loss,  # phrase(tok) classification loss
            result_dict
        )

    @torch.no_grad()
    def evaluate(self, quiet=True):
        self.model.eval()
        self.phrase_encoder.eval()
        data_dir = f'{self.args["training_data_dir"]}/dev'
        gpt_batch, gpt_label = [], []
        query_text, phrase_pos = [], []
        with open(f'{data_dir}/dev_query_tok.jsonl') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                line = json.loads(line)
                index, tok_label, query = line['index'], line['results'], line['query']
                toks, labels, all_pos = [], [], []
                for tok, label, pos in tok_label:
                    toks.append(tok)
                    labels.append(label)
                    all_pos.append(pos)
                query_text.append(query)
                gpt_batch.append(toks)
                gpt_label.append(labels)
                phrase_pos.append(all_pos)
        gpt_reps_list, query_tok_list, gpt_label_list, suffix_pos_list = self.encode_query_ids_batch(gpt_batch, gpt_label, phrase_pos, batch_size=32, quiet=quiet)
        bert_batch, bert_indexs = [], []
        with open(f'{data_dir}/dev_document_tok.jsonl') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                line = json.loads(line)
                index, toks, idxs = line
                bert_batch.append(toks)
                bert_indexs.append(idxs)
        phrase_reps_list, all_phrases = self.encode_doc_ids_batch(bert_batch, bert_indexs, batch_size=80, quiet=quiet)
        logits, indexs = [], []
        topk = 20
        for gpt_reps in tqdm(gpt_reps_list, desc='Computing IP', disable=quiet):
            gpt_reps = gpt_reps.cuda()
            topk_logits = None
            topk_indexs = None
            cur_pos = self.vocab_size 
            for phrase_reps in phrase_reps_list:
                phrase_reps = phrase_reps.cuda()
                logits_ = torch.matmul(gpt_reps, phrase_reps.t())
                topk_logits_, topk_indexs_ = torch.topk(logits_, k=topk, dim=-1)
                topk_logits_ = topk_logits_.cpu()
                topk_indexs_ = topk_indexs_.cpu()
                topk_indexs_ += cur_pos
                cur_pos += phrase_reps.shape[0]
                if topk_logits is None:
                    topk_logits = topk_logits_
                    topk_indexs = topk_indexs_
                else:
                    topk_logits = torch.cat([topk_logits, topk_logits_], dim=-1)
                    topk_indexs = torch.cat([topk_indexs, topk_indexs_], dim=-1)
            # token
            tok_reps = self.token_embeddings
            if self.dim_proj is not None:
                tok_reps = self.dim_proj(tok_reps)
            logits_ = torch.matmul(gpt_reps, tok_reps.t())
            topk_logits_, topk_indexs_ = torch.topk(logits_, k=topk, dim=-1)
            topk_logits_ = topk_logits_.cpu()
            topk_indexs_ = topk_indexs_.cpu()
            topk_logits = torch.cat([topk_logits, topk_logits_], dim=-1)
            topk_indexs = torch.cat([topk_indexs, topk_indexs_], dim=-1)
            logits.append(topk_logits)
            indexs.append(topk_indexs)
        
        tok_counter, phrase_counter = 0, 0
        all_result = collections.defaultdict(int)
        k_list = [0, 1, 3, 5, 10, 20]
        for logit, index, q_tok, label, suffix_pos in tqdm(zip(logits, indexs, query_tok_list, gpt_label_list, suffix_pos_list), desc='Evaluating', disable=quiet):
            topk_logits_, topk_indexs_ = torch.topk(logit, k=topk, dim=-1)
            real_indexs_ = index[torch.tensor(range(index.shape[0])).view(-1, 1), topk_indexs_]
            for i in range(real_indexs_.shape[0]):
                topk_pred_ = real_indexs_[i].cpu().tolist()
                label_ = label[i]
                tok_ = q_tok[i]
                if label_ < self.vocab_size: # token
                    tok_counter += 1
                    for k_idx in range(len(k_list) - 1):
                        st, end = k_list[k_idx], k_list[k_idx + 1]
                        if label_ in topk_pred_[st: end]:
                            for later_end in range(k_idx + 1, len(k_list)):
                                all_result[f'token_hit@{k_list[later_end]}'] += 1
                                all_result[f'global_acc@{k_list[later_end]}'] += 1
                            break
                else:
                    phrase_counter += 1
                    # hit
                    for k_idx in range(len(k_list) - 1):
                        st, end = k_list[k_idx], k_list[k_idx + 1]
                        if label_ in topk_pred_[st: end]:
                            for later_end in range(k_idx + 1, len(k_list)):
                                all_result[f'phrase_hit@{k_list[later_end]}'] += 1
                            break
                    
                    pos_ = suffix_pos[i]
                    assert pos_[1] != -1
                    suffix_ = query_text[pos_[0]][pos_[1]:] + ' '
                    # print(suffix_)

                    # target_phrase = all_phrases[label_ - self.vocab_size]
                    # # print('target_phrase:', target_phrase)

                    # recall & acc
                    valid_counter = 0
                    for idx in range(topk):
                        pred_idx_ = topk_pred_[idx]
                        if (pred_idx_ >= self.vocab_size and suffix_.startswith(all_phrases[pred_idx_ - self.vocab_size] + ' ')) \
                            or (pred_idx_ < self.vocab_size and pred_idx_ == tok_):
                            # pred_phrase_ = all_phrases[pred_idx_ - self.vocab_size]
                            # print('pred_phrase_:', pred_phrase_)
                            valid_counter += 1
                            if valid_counter == 1:
                                for tmp in range(idx, topk):
                                    if tmp + 1 in k_list[1:]: 
                                        all_result[f'phrase_acc@{tmp + 1}'] += 1
                                        all_result[f'global_acc@{tmp + 1}'] += 1
                        # Recall     
                        # if idx + 1 in k_list[1:]:
                        #     all_result[f'phrase_recall@{idx + 1}'] += valid_counter

        # for k, v in all_result.items():
        #     print('%s: %.4f' % (k, v / counter))
        return all_result, tok_counter, phrase_counter

    @torch.no_grad()
    def analysis_emb(self, quiet=True):
        data_dir = f'{self.args["training_data_dir"]}/dev'
        bert_batch, bert_indexs = [], []
        with open(f'{data_dir}/dev_document_tok.jsonl') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                line = json.loads(line)
                index, toks, idxs = line
                bert_batch.append(toks)
                bert_indexs.append(idxs)
        phrase_reps_list, all_phrases = self.encode_doc_ids_batch(bert_batch, bert_indexs, batch_size=80, quiet=quiet)
        phrase_reps = torch.vstack(phrase_reps_list)
        mean = torch.mean(phrase_reps, dim=0)
        std = torch.std(phrase_reps, dim=0)
        phrase_num = phrase_reps.shape[0]
        return phrase_num, mean, std

    @torch.no_grad()
    def encode_query_ids_batch(self, gpt_batch, gpt_label, phrase_pos, batch_size=128, device='cuda', quiet=True):
        all_query_reps = []
        all_query_toks = []
        all_gpt2_labels = []
        suffix_pos_list = []
        for st in tqdm(range(0, len(gpt_batch), batch_size), desc='Encoding queries', disable=quiet):
            end = min(st + batch_size, len(gpt_batch))
            gpt2_ids = gpt_batch[st: end]
            labels = gpt_label[st: end]
            gpt2_ids_cpu = pad_sequence([torch.LongTensor(i) for i in gpt2_ids], padding_value=self.tokenizer.eos_token_id, batch_first=True)
            max_length = gpt2_ids_cpu.shape[1]
            suffix_pos = []
            for all_pos in phrase_pos[st: end]:
                suffix_pos.extend(all_pos[1:] + ["" for _ in range(max_length - len(all_pos))])

            gpt2_ids = gpt2_ids_cpu.cuda()
            gpt2_mask = generate_mask(gpt2_ids, pad_token_idx=self.tokenizer.eos_token_id).cuda()
            query_reps = self.model(input_ids=gpt2_ids, attention_mask=gpt2_mask, output_hidden_states=True).hidden_states[-1]
            if self.dim_proj is not None:
                query_reps = self.dim_proj(query_reps)
            gpt2_labels_cpu = pad_sequence([torch.LongTensor(i) for i in labels], padding_value=self.tokenizer.eos_token_id, batch_first=True)
            query_reps = query_reps.cpu()
            valid_mask = gpt2_mask[:, 1:].reshape(-1).to(torch.bool).cpu()
            query_reps = query_reps[:, :-1].reshape(-1, query_reps.shape[-1])[valid_mask]
            gpt2_labels_cpu = gpt2_labels_cpu[:, 1:].reshape(-1)[valid_mask]

            valid_idx = torch.nonzero(valid_mask).view(-1).tolist()
            suffix_pos = [suffix_pos[idx] for idx in valid_idx]

            all_query_reps.append(query_reps)
            all_query_toks.append(gpt2_ids_cpu[:, :-1].reshape(-1)[valid_mask])
            all_gpt2_labels.append(gpt2_labels_cpu)
            suffix_pos_list.append(suffix_pos)
        return all_query_reps, all_query_toks, all_gpt2_labels, suffix_pos_list
    
    @torch.no_grad()
    def encode_doc_ids_batch(self, bert_batch, bert_indexs, batch_size=128, device='cuda', quiet=True):
        phrase_reps = []
        all_phrases = []
        for st in tqdm(range(0, len(bert_batch), batch_size), desc='Encoding docs', disable=quiet):
            end = min(st + batch_size, len(bert_batch))
            bert_ids = bert_batch[st: end]
            indexs = bert_indexs[st: end]

            max_bert_length = max([len(i) for i in bert_ids])
            start_idxs, end_idxs = [], []
            phrases_ = []
            for doc_idx, (ids, all_idx) in enumerate(zip(bert_ids, indexs)):
                for st_idx_, end_idx_, phrase_str in all_idx:
                    start_idxs.append(st_idx_ + doc_idx * max_bert_length)
                    end_idxs.append(end_idx_ + doc_idx * max_bert_length)
                    phrases_.append(phrase_str)
            start_idxs = torch.LongTensor(start_idxs).cuda()
            end_idxs = torch.LongTensor(end_idxs).cuda()

            bert_ids = pad_sequence([torch.LongTensor(i) for i in bert_ids], padding_value=self.bert_tokenizer.pad_token_id, batch_first=True).cuda()
            bert_mask = generate_mask(bert_ids, pad_token_idx=self.bert_tokenizer.pad_token_id).cuda()

            output = self.phrase_encoder(bert_ids, bert_mask, output_hidden_states=True)['hidden_states'][-1]  # [B, S, E]
            s_rep = self.s_proj(output)
            e_rep = self.e_proj(output)
            s_rep = s_rep.reshape(-1, s_rep.size(-1))
            e_rep = e_rep.reshape(-1, e_rep.size(-1))  # [B_doc*S_doc, 768//2]
            doc_phrase_st_rep = s_rep[start_idxs]
            doc_phrase_end_rep = e_rep[end_idxs]
            doc_phrase_rep = torch.cat([doc_phrase_st_rep, doc_phrase_end_rep], dim=-1)
            if self.dim_proj is not None:
                doc_phrase_rep = self.dim_proj(doc_phrase_rep)
            phrase_reps.append(doc_phrase_rep.cpu())
            all_phrases.extend(phrases_)
        return phrase_reps, all_phrases

    @torch.no_grad()
    def encode_doc_batch(self, sentences, batch_size=128, device='cuda'):
        all_s_rep = []
        all_e_rep = []
        all_offsets = []
        for st_idx in range(0, len(sentences), batch_size):
            end_idx = min(len(sentences), st_idx + batch_size)
            batch_sentences = sentences[st_idx: end_idx]
            encoded_dict = self.bert_tokenizer(
                batch_sentences,
                max_length=512,
                return_tensors='pt',
                padding='longest',
                return_offsets_mapping=True
            )
            offset_mapping = encoded_dict['offset_mapping']
            all_offsets.extend(offset_mapping)

            encoded_dict = {k: v.to(device) for k, v in encoded_dict.items()}
            output = self.phrase_encoder(**{k: v for k, v in encoded_dict.items() if k != 'offset_mapping'}, output_hidden_states=True)['hidden_states'][-1]  # [B, S, E]
            # collect the phrase start representations and phrase end representations
            s_rep = self.s_proj(output)
            e_rep = self.e_proj(output)
            all_s_rep.extend(s_rep)
            all_e_rep.extend(e_rep)
        return all_s_rep, all_e_rep, all_offsets

    @torch.no_grad()
    def encode_query_batch(self, sentences, batch_size=256, device='cuda'):
        self.tokenizer.pad_token = self.tokenizer.eos_token_id
        all_rep = []
        all_offsets = []
        for st_idx in range(0, len(sentences), batch_size):
            end_idx = min(len(sentences), st_idx + batch_size)
            batch_sentences = sentences[st_idx: end_idx]
            encoded_dict = self.tokenizer(
                batch_sentences,
                max_length=512,
                return_tensors='pt',
                padding='longest',
                return_offsets_mapping=True
            )

            encoded_dict = {k: v.to(device) for k, v in encoded_dict.items()}
            offset_mapping = encoded_dict['offset_mapping']
            all_offsets.extend(offset_mapping)

            output = self.model(**{k: v for k, v in encoded_dict.items() if k != 'offset_mapping'}, output_hidden_states=True)['hidden_states'][-1]  # [B, S, E]
            # collect the phrase start representations and phrase end representations
            all_rep.extend(output)
        return all_rep, all_offsets