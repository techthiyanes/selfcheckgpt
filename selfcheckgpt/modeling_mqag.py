import re
from typing import Dict, List, Set, Tuple, Union
import numpy as np
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from transformers import LongformerTokenizer, LongformerForMultipleChoice, LongformerForSequenceClassification
from .utils import prepare_qa_input, prepare_distractor_input, prepare_answering_input

# ---------------------------------------------------------------------------------------- #
# Functions for counting
def method_simple_counting(
        prob,
        u_score,
        prob_s,
        u_score_s,
        num_samples,
        AT,
    ):
    """
    simple counting method score => count_mismatch / (count_match + count_mismatch)
    :return score: 'inconsistency' score
    """
    # bad questions, i.e. not answerable given the passage
    if u_score < AT:
        return 0.5
    a_DT = np.argmax(prob)
    count_good_sample, count_match = 0, 0
    for s in range(num_samples):
        if u_score_s[s] >= AT:
            count_good_sample += 1
            a_S = np.argmax(prob_s[s])
            if a_DT == a_S:
                count_match += 1
    if count_good_sample == 0:
        score = 0.5
    else:
        score = (count_good_sample-count_match) / count_good_sample
    return score

def method_vanilla_bayes(
        prob,
        u_score,
        prob_s,
        u_score_s,
        num_samples,
        beta1, beta2, AT,
    ):
    """
    (vanilla) bayes method score: compute P(sentence is non-factual | count_match, count_mismatch)
    :return score: 'inconsistency' score
    """
    if u_score < AT:
        return 0.5
    a_DT = np.argmax(prob)
    count_match, count_mismatch = 0, 0
    for s in range(num_samples):
        if u_score_s[s] >= AT:
            a_S = np.argmax(prob_s[s])
            if a_DT == a_S:
                count_match += 1
            else:
                count_mismatch += 1
    gamma1 = beta2 / (1.0-beta1)
    gamma2 = beta1 / (1.0-beta2)
    score = (gamma2**count_mismatch) / ((gamma1**count_match) + (gamma2**count_mismatch))
    return score

def method_bayes_with_alpha(
        prob,
        u_score,
        prob_s,
        u_score_s,
        num_samples,
        beta1, beta2,
    ):
    """
    bayes method (with answerability score, i.e. soft-counting) score
    :return score: 'inconsistency' score
    """
    a_DT = np.argmax(prob)
    count_match, count_mismatch = 0, 0
    for s in range(num_samples):
        ans_score = u_score_s[s]
        a_S = np.argmax(prob_s[s])
        if a_DT == a_S:
            count_match += ans_score
        else:
            count_mismatch += ans_score
    gamma1 = beta2 / (1.0-beta1)
    gamma2 = beta1 / (1.0-beta2)
    score = (gamma2**count_mismatch) / ((gamma1**count_match) + (gamma2**count_mismatch))
    return score

# ---------------------------------------------------------------------------------------- #
# Functions for Question Generation & Answering

def question_generation_sentence_level(
        g1_model,
        g1_tokenizer,
        g2_model,
        g2_tokenizer,
        sentence,
        passage,
        num_questions_per_sent,
        device,
    ):
    qa_input_ids = prepare_qa_input(
            g1_tokenizer,
            context=sentence,
            device=device,
    )
    num_valid_questions = 0
    questions = []
    for q_ in range(num_questions_per_sent):
        # Stage G.1: question+answer generation
        outputs = g1_model.generate(
            qa_input_ids,
            max_new_tokens=128,
            do_sample=True,
        )
        question_answer = g1_tokenizer.decode(outputs[0], skip_special_tokens=False)
        question_answer = question_answer.replace(g1_tokenizer.pad_token, "").replace(g1_tokenizer.eos_token, "")
        question_answer_split = question_answer.split(g1_tokenizer.sep_token)
        if len(question_answer_split) == 2:
            # valid Question + Annswer output
            num_valid_questions += 1
        else:
            continue
        question = question_answer_split[0].strip()
        answer = question_answer_split[1].strip()

        # Stage G.2: Distractor Generation
        distractor_input_ids = prepare_distractor_input(
            g2_tokenizer,
            context = passage,
            question = question,
            answer = answer,
            device = device,
            separator = g2_tokenizer.sep_token,
        )
        outputs = g2_model.generate(
            distractor_input_ids,
            max_new_tokens=128,
            do_sample=True,
        )
        distractors = g2_tokenizer.decode(outputs[0], skip_special_tokens=False)
        distractors = distractors.replace(g2_tokenizer.pad_token, "").replace(g2_tokenizer.eos_token, "")
        distractors = re.sub("<extra\S+>", g2_tokenizer.sep_token, distractors)
        distractors = [y.strip() for y in distractors.split(g2_tokenizer.sep_token)]
        options = [answer] + distractors

        while len(options) < 4:
            # print("Warning: options =", options)
            options.append(options[-1])

        question_item = {
            'question': question,
            'options': options,
        }
        questions.append(question_item)
    return questions

def answering(
        a_model,
        a_tokenizer,
        question,
        options,
        context,
        max_seq_length,
        device,
    ):
    answering_given_passage = prepare_answering_input(
        tokenizer=a_tokenizer,
        question=question,
        options=options,
        context=context,
        device=device,
        max_seq_length=max_seq_length,
    )
    answering_outputs = a_model(**answering_given_passage)
    prob = torch.softmax(answering_outputs['logits'], dim=-1)[0].cpu().numpy()
    return prob

def answerability_scoring(
        u_model,
        u_tokenizer,
        question,
        context,
        max_length,
        device,
    ):
    """
    :return prob: prob -> 0.0 means unanswerable, prob -> 1.0 means answerable
    """
    input_text = question + ' ' + u_tokenizer.sep_token + ' ' + context
    inputs = u_tokenizer(input_text, max_length=max_length, truncation=True, return_tensors="pt")
    inputs = inputs.to(device)
    logits = u_model(**inputs).logits
    logits = logits.squeeze(-1)
    prob = torch.sigmoid(logits).item()
    return prob

# ---------------------------------------------------------------------------------------- #
# Main SelfCheckMQAG class

class SelfCheckMQAG:
    def __init__(self, device=None):
        # Question Generation Systems (G1 & G2)
        self.g1_tokenizer = AutoTokenizer.from_pretrained("potsawee/t5-large-generation-squad-QuestionAnswer")
        self.g1_model = AutoModelForSeq2SeqLM.from_pretrained("potsawee/t5-large-generation-squad-QuestionAnswer")
        self.g2_tokenizer = AutoTokenizer.from_pretrained("potsawee/t5-large-generation-race-Distractor")
        self.g2_model = AutoModelForSeq2SeqLM.from_pretrained("potsawee/t5-large-generation-race-Distractor")

        # Question Answering System (A)
        self.a_tokenizer = LongformerTokenizer.from_pretrained("potsawee/longformer-large-4096-answering-race")
        self.a_model = LongformerForMultipleChoice.from_pretrained("potsawee/longformer-large-4096-answering-race")

        # (Un)Answerability System (U)
        self.u_tokenizer = LongformerTokenizer.from_pretrained("potsawee/longformer-large-4096-answerable-squad2")
        self.u_model = LongformerForSequenceClassification.from_pretrained("potsawee/longformer-large-4096-answerable-squad2")

        self.g1_model.eval()
        self.g2_model.eval()
        self.a_model.eval()
        self.u_model.eval()

        if device is None:
            device = torch.device("cpu")
        self.g1_model.to(device)
        self.g2_model.to(device)
        self.a_model.to(device)
        self.u_model.to(device)
        self.device = device
        print("SelfCheck-MQAG initialized to device", device)

    @torch.no_grad()
    def predict(
            self,
            sentences: List[str],
            passage: str,
            sampled_passages: List[str],
            num_questions_per_sent: int = 5,
            scoring_method: str = "bayes_with_alpha",
            **kwargs,
        ):
        """
        This function takes sentences (to be evaluated) with sampled passages (evidence), and return sent-level scores
        :param sentences: list[str] -- sentences to be evaluated, e.g. GPT text response spilt by spacy
        :param passage: str -- the passage to be evaluated, note that splitting(passage) ---> sentences
        :param sampled_passages: list[str] -- stochastically generated responses (without sentence splitting)
        :param num_questions_per_sent: int -- number of quetions to be generated per sentence
        :return sent_scores: sentence-level score of the same length as len(sentences) # inconsistency_score, i.e. higher means likely hallucination
        """
        assert scoring_method in ['counting', 'bayes', 'bayes_with_alpha']
        num_samples = len(sampled_passages)
        sent_scores = []
        for sentence in sentences:

            # Question + Choices Generation
            questions = question_generation_sentence_level(
                self.g1_model, self.g1_tokenizer,
                self.g2_model, self.g2_tokenizer,
                sentence, passage, num_questions_per_sent, self.device)

            # Answering
            scores = []
            max_seq_length = 4096 # answering & answerability max length
            for question_item in questions:
                question, options = question_item['question'], question_item['options']

                # response
                prob = answering(
                    self.a_model, self.a_tokenizer,
                    question, options, passage,
                    max_seq_length, self.device)

                u_score = answerability_scoring(
                    self.u_model, self.u_tokenizer,
                    question, passage,
                    max_seq_length, self.device)

                prob_s = np.zeros((num_samples, 4))
                u_score_s = np.zeros((num_samples,))
                for si, sampled_passage in enumerate(sampled_passages):

                    # sample
                    prob_s[si] = answering(
                        self.a_model, self.a_tokenizer,
                        question, options, sampled_passage,
                        max_seq_length, self.device)
                    u_score_s[si] = answerability_scoring(
                        self.u_model, self.u_tokenizer,
                        question, sampled_passage,
                        max_seq_length, self.device)

                # doing comparision
                if scoring_method == 'counting':
                    score = method_simple_counting(prob, u_score, prob_s, u_score_s, num_samples, AT=kwargs['AT'])
                elif scoring_method == 'bayes':
                    score = method_vanilla_bayes(prob, u_score, prob_s, u_score_s, num_samples, beta1=kwargs['beta1'], beta2=kwargs['beta2'], AT=kwargs['AT'])
                elif scoring_method == 'bayes_with_alpha':
                    score = method_bayes_with_alpha(prob, u_score, prob_s, u_score_s, num_samples, beta1=kwargs['beta1'], beta2=kwargs['beta2'])
                scores.append(score)
            sent_score = np.mean(scores)
            sent_scores.append(sent_score)

        return np.array(sent_scores)
