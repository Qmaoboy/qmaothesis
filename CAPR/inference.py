from transformers import AutoTokenizer, AutoModelForCausalLM
from torch import nn
from accelerate import Accelerator
from peft import PeftConfig, PeftModel
from trl import AutoModelForCausalLMWithValueHead, PPOConfig, PPOTrainer
from qadataset_dataloader import qadataset_dataloader,eval_dataloader
from torch.nn.parallel import DistributedDataParallel as DDP
from tqdm import tqdm,trange
from trl import PPOConfig,PPOTrainer
import torch.multiprocessing as t_mp
import torch.distributed as dist
from torch.optim.lr_scheduler import ConstantLR,ExponentialLR,SequentialLR,StepLR
import json,copy
import numpy as np
from random import randint
from util import search_wikipedia_byurl
from prompt_strategy import prompter
from sklearn.metrics import roc_auc_score
from RL_env import Environment,reward_function,rl_writer,Parallel_Environment
import glob,os,torch,yaml
from huggingface_hub import login
import json
if os.path.isfile("api_key.yml"):
    with open("api_key.yml","r") as f:
        key=yaml.safe_load(f)

if os.path.isfile("default_config.yaml"):
    with open("default_config.yaml","r") as f:
        ac_config=yaml.safe_load(f)

login(token=key['hugginface']["token"])

os.environ['TOKENIZERS_PARALLELISM'] = 'true'

device = 'cuda:0' if torch.cuda.is_available() else 'cpu'

def Get_auroc(accuracy,confidence_scores):
    y_true=np.where(np.array(accuracy) < 0.3,0,1)
    try:
        auroc_score=roc_auc_score(np.array(y_true), np.array(confidence_scores))
    except:
        auroc_score=0

    return auroc_score

class inference:
    def __init__(self,pretrained_path,dataset_path,api_model,Save_result_path) -> None:
        torch.cuda.empty_cache()
        self.result={}
        self.model,self.tokenizer=self.load_from_pretrained(pretrained_path)
        self.dataset_path=dataset_path
        self.Save_result_path=Save_result_path
        self.api_model=api_model
        self.pace_ece,self.Verbalized_ece,self.Accuracy=[],[],[]
        self.Pace_Conf,self.Verbalized_conf=[],[]
        self.auroc,self.capr_auroc=[],[]
        self.prompter=prompter()
        self.Load_checkpoint()
        self.example={
            "api_model":self.api_model,
            "old_prompt":[],
            "new_prompt":[],
            "Ground_truth":[],
            "Document":[],
            'Result':[]
        }
        self.generation_kwargs = {
        "min_length": -1,
        'temperature': 1,
        "max_length": 256,
        # "max_new_tokens": 96, # before : 128
        "top_k": 50,
        "top_p": 0.9,
        "do_sample": True,
        "pad_token_id": self.tokenizer.eos_token_id,
        'no_repeat_ngram_size':4
        }

        if 'gpt' in self.api_model:
            self.trian_batch_size=50
            self.data_limit=0
        elif 'claude' in self.api_model:
            self.trian_batch_size=1
            self.data_limit=49

    def Load_checkpoint(self):
        if os.path.isfile(self.Save_result_path):
            with open(self.Save_result_path,'r') as f:
                self.result=json.load(f)
        else:
            self.result={}

    def question_to_prompt(self,question,document,task="Long_QA",stretagy='vanilla'):
        self.prompter.setup_task(task)
        return self.prompter.get_prompt(question,document,stretagy)

    def load_from_pretrained(self,pretrained_model_path):
        base_model_name = "meta-llama/Llama-2-7b-chat-hf"
        model = AutoModelForCausalLM.from_pretrained(base_model_name,token=key['hugginface']["token"],torch_dtype=torch.bfloat16,use_cache=True, device_map = device)
        model = PeftModel.from_pretrained(model, pretrained_model_path)
        # model = AutoModelForCausalLMWithValueHead.from_pretrained(pretrained_model_path,token=key['hugginface']["token"],torch_dtype=torch.float16,use_cache=True,device_map={"": current_device})

        tokenizer = AutoTokenizer.from_pretrained(pretrained_model_path,token=key['hugginface']["token"])
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
        tokenizer.padding_side = "left"
        print("="*50+"Load From Pretrained !!!"+"="*50)
        return model,tokenizer

    def get_result(self,prompt,instruction,ground_Truth,Document):
        '''
        Prompt Contain:
            system_prompt
            Instruction
            question
            input_text
            assit_prompt
        '''

        old_prompt=copy.deepcopy(prompt)
        for idx,p_instruc in enumerate(instruction):
            prompt[idx]['Instruction']=p_instruc

        result_batch=Parallel_Environment(prompt,self.api_model)

        show_index=randint(0,len(instruction)-1)
        print(old_prompt[show_index]['Instruction'])
        print(prompt[show_index]['Instruction'])
        print(result_batch[show_index])

        for idx,i in enumerate(result_batch):
            if i is not None:
                self.example["old_prompt"].append(old_prompt[idx])
                self.example["Result"].append(result_batch[idx])
                self.example["new_prompt"].append(prompt[idx])
                self.example["Ground_truth"].append(ground_Truth[idx])
                self.example["Document"].append(Document[idx])

        _,pace_ece,Verbalized_ece,Accuracy,Pace_Conf,Verbalized_conf = reward_function(result_batch,ground_Truth,Document)
        ## reward_list,Final_ece_list,ece_list,acc_list,Final_conf_list,conf_list
        for i in trange(len(Accuracy)):
            accuracy_value = Accuracy[i].item()
            if accuracy_value > 0:
                self.pace_ece.append(pace_ece[i].item())
                self.Verbalized_ece.append(Verbalized_ece[i].item())
                self.Accuracy.append(accuracy_value)
                self.Pace_Conf.append(Pace_Conf[i].item())
                self.Verbalized_conf.append(Verbalized_conf[i].item())

    def generate_result(self,instruct:list)->list:
        input_qeury_token=self.tokenizer(instruct,padding=True,truncation=True,max_length=512,return_tensors='pt').to(device)
        with torch.no_grad():
            outputs = self.model.generate(
                    **input_qeury_token,
                    **self.generation_kwargs
                )
            new_instruct = [self.tokenizer.decode(r[len(i):],skip_special_tokens=True) for r,i  in zip(outputs, input_qeury_token.input_ids)]

        return new_instruct

    def get_inference(self,):

        # Dataloader=eval_dataloader(dataset_path=self.dataset_path, batch_size=trian_batch_size, purpose='refine',tokenizer=self.tokenizer,shuffle=False)
        train_dataloader=qadataset_dataloader(dataset_path="din0s/asqa",split='dev',batch_size=self.trian_batch_size,shuffle=False).trainloader

        for idx,(batch,Ground_truth) in enumerate(progress:=tqdm(train_dataloader)):
            question=[i[0] for i in batch]
            document=[search_wikipedia_byurl([i[1]]) if i[2] else i[1] for i in batch]
            batch_prompt=[self.question_to_prompt(q,d) for q,d in zip(question,document)]
            response=self.generate_result([i["instruct"] for i in batch_prompt])

            progress.set_description_str(f"refine Prompt")
            self.get_result(batch_prompt,response,Ground_truth,document)
            if idx>=self.data_limit:
                self.auroc.append(Get_auroc([i for i in self.Accuracy], [i for i in self.Verbalized_conf]))
                self.capr_auroc.append(Get_auroc([i for i in self.Accuracy], [i for i in self.Pace_Conf]))
                break

        self.Save_File()

    def Save_File(self,):
        self.result[self.api_model]={
            'Example':self.example,
            'pace_ece':self.pace_ece,
            'Verbalized_ece':self.Verbalized_ece,
            'Accuracy':self.Accuracy,
            'Pace_Conf':self.Pace_Conf,
            "Verbalized_conf":self.Verbalized_conf,
            'Auroc':self.auroc,
            'PACE_Auroc':self.capr_auroc
            }

        with open(self.Save_result_path,'w+') as f:
            json.dump(self.result,f,indent=4)

def Show_mean_result(key,Save_result_path):
    if os.path.isfile(Save_result_path):
        with open(Save_result_path,'r') as f:
            result=json.load(f)
        for k,v in result.items():
            if k==key:
                print(k)
                for k1,v1 in v.items():
                    if k1!='Example':
                        print(f"\t{k1} : {np.mean(np.array(v1)):.6f}")

if __name__=="__main__":
    ## Setting
    deliminator='r11_withoutPACE'
    Agent_addres='Agent_weight/PPO_Agent_06122032_vanilla_f1_r11_withoutPACE_9_0.0009'
    dataset_path=f'response_result/20240601/din0s_asqa_gpt-3.5-turbo-0125_vanilla_Long_QA.json'
    Save_result_path=f"din0s_asqa_{deliminator}.json"
    api_model = 'gpt-3.5-turbo-0125'
    # api_model = 'gpt-4-turbo'
    # api_model = 'claude-3-5-sonnet-20240620'
    ##
    inf=inference(Agent_addres,dataset_path,api_model,Save_result_path)
    inf.get_inference()
    # Show_mean_result("origin",Save_result_path)
    Show_mean_result(api_model,Save_result_path)
