#!/usr/bin/env python
# coding=utf-8

# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
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
import importlib.util
import json
import os
import time
from dataclasses import dataclass
from typing import Dict

import requests
from huggingface_hub import HfFolder, hf_hub_download, list_spaces

from ..models.auto import AutoTokenizer
from ..utils import is_openai_available, is_torch_available, logging
from .base import TASK_MAPPING, TOOL_CONFIG_FILE, Tool, load_tool, supports_remote
from .prompts import CHAT_MESSAGE_PROMPT, download_prompt
from .python_interpreter import evaluate


logger = logging.get_logger(__name__)


if is_openai_available():
    import openai

if is_torch_available():
    from ..generation import StoppingCriteria, StoppingCriteriaList
    from ..models.auto import AutoModelForCausalLM
else:
    StoppingCriteria = object

_tools_are_initialized = False


BASE_PYTHON_TOOLS = {
    "print": print,
    "range": range,
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
}


@dataclass
class PreTool:
    task: str
    description: str
    repo_id: str


HUGGINGFACE_DEFAULT_TOOLS = {}


HUGGINGFACE_DEFAULT_TOOLS_FROM_HUB = [
    "image-transformation",
    "text-download",
    "text-to-image",
    "text-to-video",
]


def get_remote_tools(organization="huggingface-tools"):
    spaces = list_spaces(author=organization)
    tools = {}
    for space_info in spaces:
        repo_id = space_info.id
        resolved_config_file = hf_hub_download(repo_id, TOOL_CONFIG_FILE, repo_type="space")
        with open(resolved_config_file, encoding="utf-8") as reader:
            config = json.load(reader)

        task = repo_id.split("/")[-1]
        tools[config["name"]] = PreTool(task=task, description=config["description"], repo_id=repo_id)

    return tools


def _setup_default_tools():
    global HUGGINGFACE_DEFAULT_TOOLS
    global _tools_are_initialized

    if _tools_are_initialized:
        return

    main_module = importlib.import_module("transformers")
    tools_module = main_module.tools

    remote_tools = get_remote_tools()
    for task_name, tool_class_name in TASK_MAPPING.items():
        tool_class = getattr(tools_module, tool_class_name)
        description = tool_class.description
        HUGGINGFACE_DEFAULT_TOOLS[tool_class.name] = PreTool(task=task_name, description=description, repo_id=None)

    for task_name in HUGGINGFACE_DEFAULT_TOOLS_FROM_HUB:
        found = False
        for tool_name, tool in remote_tools.items():
            if tool.task == task_name:
                HUGGINGFACE_DEFAULT_TOOLS[tool_name] = tool
                found = True
                break

        if not found:
            raise ValueError(f"{task_name} is not implemented on the Hub.")

    _tools_are_initialized = True


def resolve_tools(code, toolbox, remote=False, cached_tools=None):
    if cached_tools is None:
        resolved_tools = BASE_PYTHON_TOOLS.copy()
    else:
        resolved_tools = cached_tools
    for name, tool in toolbox.items():
        if name not in code or name in resolved_tools:
            continue

        if isinstance(tool, Tool):
            resolved_tools[name] = tool
        else:
            task_or_repo_id = tool.task if tool.repo_id is None else tool.repo_id
            _remote = remote and supports_remote(task_or_repo_id)
            resolved_tools[name] = load_tool(task_or_repo_id, remote=_remote)

    return resolved_tools


def get_tool_creation_code(code, toolbox, remote=False):
    code_lines = ["from transformers import load_tool", ""]
    for name, tool in toolbox.items():
        if name not in code or isinstance(tool, Tool):
            continue

        task_or_repo_id = tool.task if tool.repo_id is None else tool.repo_id
        line = f'{name} = load_tool("{task_or_repo_id}"'
        if remote:
            line += ", remote=True"
        line += ")"
        code_lines.append(line)

    return "\n".join(code_lines) + "\n"


def clean_code_for_chat(result):
    lines = result.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].lstrip().startswith("```"):
        idx += 1
    explanation = "\n".join(lines[:idx]).strip()
    if idx == len(lines):
        return explanation, None

    idx += 1
    start_idx = idx
    while not lines[idx].lstrip().startswith("```"):
        idx += 1
    code = "\n".join(lines[start_idx:idx]).strip()

    return explanation, code


def clean_code_for_run(result):
    result = f"I will use the following {result}"
    explanation, code = result.split("Answer:")
    explanation = explanation.strip()
    code = code.strip()

    code_lines = code.split("\n")
    if code_lines[0] in ["```", "```py", "```python"]:
        code_lines = code_lines[1:]
    if code_lines[-1] == "```":
        code_lines = code_lines[:-1]
    code = "\n".join(code_lines)

    return explanation, code


class Agent:
    """
    Base class for all agents which contains the main API methods.

    Args:
        chat_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `chat` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `chat_prompt_template.txt` in this repo in this case.
        run_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `run` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `run_prompt_template.txt` in this repo in this case.
        additional_tools ([`Tool`], list of tools or dictionary with tool values, *optional*):
            Any additional tools to include on top of the default ones. If you pass along a tool with the same name as
            one of the default tools, that default tool will be overridden.
    """

    def __init__(self, chat_prompt_template=None, run_prompt_template=None, additional_tools=None):
        _setup_default_tools()

        agent_name = self.__class__.__name__
        self.chat_prompt_template = download_prompt(chat_prompt_template, agent_name, mode="chat")
        self.run_prompt_template = download_prompt(run_prompt_template, agent_name, mode="run")
        self._toolbox = HUGGINGFACE_DEFAULT_TOOLS.copy()
        self.log = print
        if additional_tools is not None:
            if isinstance(additional_tools, (list, tuple)):
                additional_tools = {t.name: t for t in additional_tools}
            elif not isinstance(additional_tools, dict):
                additional_tools = {additional_tools.name: additional_tools}

            replacements = {name: tool for name, tool in additional_tools.items() if name in HUGGINGFACE_DEFAULT_TOOLS}
            self._toolbox.update(additional_tools)
            if len(replacements) > 1:
                names = "\n".join([f"- {n}: {t}" for n, t in replacements.items()])
                logger.warn(
                    f"The following tools have been replaced by the ones provided in `additional_tools`:\n{names}."
                )
            elif len(replacements) == 1:
                name = list(replacements.keys())[0]
                logger.warn(f"{name} has been replaced by {replacements[name]} as provided in `additional_tools`.")

        self.prepare_for_new_chat()

    @property
    def toolbox(self) -> Dict[str, Tool]:
        """Get all tool currently available to the agent"""
        return self._toolbox

    def format_prompt(self, task, chat_mode=False):
        description = "\n".join([f"- {name}: {tool.description}" for name, tool in self.toolbox.items()])
        if chat_mode:
            if self.chat_history is None:
                prompt = self.chat_prompt_template.replace("<<all_tools>>", description)
            else:
                prompt = self.chat_history
            prompt += CHAT_MESSAGE_PROMPT.replace("<<task>>", task)
        else:
            prompt = self.run_prompt_template.replace("<<all_tools>>", description)
            prompt = prompt.replace("<<prompt>>", task)
        return prompt

    def set_stream(self, streamer):
        """
        Set the function use to stream results (which is `print` by default).

        Args:
            streamer (`callable`): The function to call when streaming results from the LLM.
        """
        self.log = streamer

    def chat(self, task, *, return_code=False, remote=False, **kwargs):
        """
        Sends a new request to the agent in a chat. Will use the previous ones in its history.

        Args:
            task (`str`): The task to perform
            return_code (`bool`, *optional*, defaults to `False`):
                Whether to just return code and not evaluate it.
            remote (`bool`, *optional*, defaults to `False`):
                Whether or not to use remote tools (inference endpoints) instead of local ones.
            kwargs (additional keyword arguments, *optional*):
                Any keyword argument to send to the agent when evaluating the code.

        Example:

        ```py
        from transformers import HfAgent

        agent = HfAgent("https://api-inference.huggingface.co/models/bigcode/starcoder")
        agent.chat("Draw me a picture of rivers and lakes")

        agent.chat("Transform the picture so that there is a rock in there")
        ```
        """
        prompt = self.format_prompt(task, chat_mode=True)
        result = self.generate_one(prompt, stop=["Human:", "====="])
        self.chat_history = prompt + result.strip() + "\n"
        explanation, code = clean_code_for_chat(result)

        self.log(f"==Explanation from the agent==\n{explanation}")

        if code is not None:
            self.log(f"\n\n==Code generated by the agent==\n{code}")
            if not return_code:
                self.log("\n\n==Result==")
                self.cached_tools = resolve_tools(code, self.toolbox, remote=remote, cached_tools=self.cached_tools)
                self.chat_state.update(kwargs)
                return evaluate(code, self.cached_tools, self.chat_state, chat_mode=True)
            else:
                tool_code = get_tool_creation_code(code, self.toolbox, remote=remote)
                return f"{tool_code}\n{code}"

    def prepare_for_new_chat(self):
        """
        Clears the history of prior calls to [`~Agent.chat`].
        """
        self.chat_history = None
        self.chat_state = {}
        self.cached_tools = None

    def run(self, task, *, return_code=False, remote=False, **kwargs):
        """
        Sends a request to the agent.

        Args:
            task (`str`): The task to perform
            return_code (`bool`, *optional*, defaults to `False`):
                Whether to just return code and not evaluate it.
            remote (`bool`, *optional*, defaults to `False`):
                Whether or not to use remote tools (inference endpoints) instead of local ones.
            kwargs (additional keyword arguments, *optional*):
                Any keyword argument to send to the agent when evaluating the code.

        Example:

        ```py
        from transformers import HfAgent

        agent = HfAgent("https://api-inference.huggingface.co/models/bigcode/starcoder")
        agent.run("Draw me a picture of rivers and lakes")
        ```
        """
        prompt = self.format_prompt(task)
        result = self.generate_one(prompt, stop=["Task:"])
        explanation, code = clean_code_for_run(result)

        self.log(f"==Explanation from the agent==\n{explanation}")

        self.log(f"\n\n==Code generated by the agent==\n{code}")
        if not return_code:
            self.log("\n\n==Result==")
            self.cached_tools = resolve_tools(code, self.toolbox, remote=remote, cached_tools=self.cached_tools)
            return evaluate(code, self.cached_tools, state=kwargs.copy())
        else:
            tool_code = get_tool_creation_code(code, self.toolbox, remote=remote)
            return f"{tool_code}\n{code}"

    def generate_one(self, prompt, stop):
        # This is the method to implement in your custom agent.
        raise NotImplementedError

    def generate_many(self, prompts, stop):
        # Override if you have a way to do batch generation faster than one by one
        return [self.generate_one(prompt, stop) for prompt in prompts]


class OpenAiAgent(Agent):
    """
    Agent that uses the openai API to generate code.

    <Tip warning={true}>

    The openAI models are used in generation mode, so even for the `chat()` API, it's better to use models like
    `"text-davinci-003"` over the chat-GPT variant. Proper support for chat-GPT models will come in a next version.

    </Tip>

    Args:
        model (`str`, *optional*, defaults to `"text-davinci-003"`):
            The name of the OpenAI model to use.
        api_key (`str`, *optional*):
            The API key to use. If unset, will look for the environment variable `"OPENAI_API_KEY"`.
        chat_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `chat` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `chat_prompt_template.txt` in this repo in this case.
        run_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `run` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `run_prompt_template.txt` in this repo in this case.
        additional_tools ([`Tool`], list of tools or dictionary with tool values, *optional*):
            Any additional tools to include on top of the default ones. If you pass along a tool with the same name as
            one of the default tools, that default tool will be overridden.

    Example:

    ```py
    from transformers import OpenAiAgent

    agent = OpenAiAgent(model="text-davinci-003", api_key=xxx)
    agent.run("Is the following `text` (in Spanish) positive or negative?", text="¡Este es un API muy agradable!")
    ```
    """

    def __init__(
        self,
        model="text-davinci-003",
        api_key=None,
        chat_prompt_template=None,
        run_prompt_template=None,
        additional_tools=None,
    ):
        if not is_openai_available():
            raise ImportError("Using `OpenAiAgent` requires `openai`: `pip install openai`.")

        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY", None)
        if api_key is None:
            raise ValueError(
                "You need an openai key to use `OpenAIAgent`. You can get one here: Get one here "
                "https://openai.com/api/`. If you have one, set it in your env with `os.environ['OPENAI_API_KEY'] = "
                "xxx."
            )
        else:
            openai.api_key = api_key
        self.model = model
        super().__init__(
            chat_prompt_template=chat_prompt_template,
            run_prompt_template=run_prompt_template,
            additional_tools=additional_tools,
        )

    def generate_many(self, prompts, stop):
        if "gpt" in self.model:
            return [self._chat_generate(prompt, stop) for prompt in prompts]
        else:
            return self._completion_generate(prompts, stop)

    def generate_one(self, prompt, stop):
        if "gpt" in self.model:
            return self._chat_generate(prompt, stop)
        else:
            return self._completion_generate([prompt], stop)[0]

    def _chat_generate(self, prompt, stop):
        result = openai.ChatCompletion.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            stop=stop,
        )
        return result["choices"][0]["message"]["content"]

    def _completion_generate(self, prompts, stop):
        result = openai.Completion.create(
            model=self.model,
            prompt=prompts,
            temperature=0,
            stop=stop,
            max_tokens=200,
        )
        return [answer["text"] for answer in result["choices"]]


class AzureOpenAiAgent(Agent):
    """
    Agent that uses Azure OpenAI to generate code. See the [official
    documentation](https://learn.microsoft.com/en-us/azure/cognitive-services/openai/) to learn how to deploy an openAI
    model on Azure

    <Tip warning={true}>

    The openAI models are used in generation mode, so even for the `chat()` API, it's better to use models like
    `"text-davinci-003"` over the chat-GPT variant. Proper support for chat-GPT models will come in a next version.

    </Tip>

    Args:
        deployment_id (`str`):
            The name of the deployed Azure openAI model to use.
        api_key (`str`, *optional*):
            The API key to use. If unset, will look for the environment variable `"AZURE_OPENAI_API_KEY"`.
        resource_name (`str`, *optional*):
            The name of your Azure OpenAI Resource. If unset, will look for the environment variable
            `"AZURE_OPENAI_RESOURCE_NAME"`.
        api_version (`str`, *optional*, default to `"2022-12-01"`):
            The API version to use for this agent.
        is_chat_mode (`bool`, *optional*):
            Whether you are using a completion model or a chat model (see note above, chat models won't be as
            efficient). Will default to `gpt` being in the `deployment_id` or not.
        chat_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `chat` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `chat_prompt_template.txt` in this repo in this case.
        run_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `run` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `run_prompt_template.txt` in this repo in this case.
        additional_tools ([`Tool`], list of tools or dictionary with tool values, *optional*):
            Any additional tools to include on top of the default ones. If you pass along a tool with the same name as
            one of the default tools, that default tool will be overridden.

    Example:

    ```py
    from transformers import AzureOpenAiAgent

    agent = AzureAiAgent(deployment_id="Davinci-003", api_key=xxx, resource_name=yyy)
    agent.run("Is the following `text` (in Spanish) positive or negative?", text="¡Este es un API muy agradable!")
    ```
    """

    def __init__(
        self,
        deployment_id,
        api_key=None,
        resource_name=None,
        api_version="2022-12-01",
        is_chat_model=None,
        chat_prompt_template=None,
        run_prompt_template=None,
        additional_tools=None,
    ):
        if not is_openai_available():
            raise ImportError("Using `OpenAiAgent` requires `openai`: `pip install openai`.")

        self.deployment_id = deployment_id
        openai.api_type = "azure"
        if api_key is None:
            api_key = os.environ.get("AZURE_OPENAI_API_KEY", None)
        if api_key is None:
            raise ValueError(
                "You need an Azure openAI key to use `AzureOpenAIAgent`. If you have one, set it in your env with "
                "`os.environ['AZURE_OPENAI_API_KEY'] = xxx."
            )
        else:
            openai.api_key = api_key
        if resource_name is None:
            resource_name = os.environ.get("AZURE_OPENAI_RESOURCE_NAME", None)
        if resource_name is None:
            raise ValueError(
                "You need a resource_name to use `AzureOpenAIAgent`. If you have one, set it in your env with "
                "`os.environ['AZURE_OPENAI_RESOURCE_NAME'] = xxx."
            )
        else:
            openai.api_base = f"https://{resource_name}.openai.azure.com"
        openai.api_version = api_version

        if is_chat_model is None:
            is_chat_model = "gpt" in deployment_id.lower()
        self.is_chat_model = is_chat_model

        super().__init__(
            chat_prompt_template=chat_prompt_template,
            run_prompt_template=run_prompt_template,
            additional_tools=additional_tools,
        )

    def generate_many(self, prompts, stop):
        if self.is_chat_model:
            return [self._chat_generate(prompt, stop) for prompt in prompts]
        else:
            return self._completion_generate(prompts, stop)

    def generate_one(self, prompt, stop):
        if self.is_chat_model:
            return self._chat_generate(prompt, stop)
        else:
            return self._completion_generate([prompt], stop)[0]

    def _chat_generate(self, prompt, stop):
        result = openai.ChatCompletion.create(
            engine=self.deployment_id,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            stop=stop,
        )
        return result["choices"][0]["message"]["content"]

    def _completion_generate(self, prompts, stop):
        result = openai.Completion.create(
            engine=self.deployment_id,
            prompt=prompts,
            temperature=0,
            stop=stop,
            max_tokens=200,
        )
        return [answer["text"] for answer in result["choices"]]


class HfAgent(Agent):
    """
    Agent that uses an inference endpoint to generate code.

    Args:
        url_endpoint (`str`):
            The name of the url endpoint to use.
        token (`str`, *optional*):
            The token to use as HTTP bearer authorization for remote files. If unset, will use the token generated when
            running `huggingface-cli login` (stored in `~/.huggingface`).
        chat_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `chat` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `chat_prompt_template.txt` in this repo in this case.
        run_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `run` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `run_prompt_template.txt` in this repo in this case.
        additional_tools ([`Tool`], list of tools or dictionary with tool values, *optional*):
            Any additional tools to include on top of the default ones. If you pass along a tool with the same name as
            one of the default tools, that default tool will be overridden.

    Example:

    ```py
    from transformers import HfAgent

    agent = HfAgent("https://api-inference.huggingface.co/models/bigcode/starcoder")
    agent.run("Is the following `text` (in Spanish) positive or negative?", text="¡Este es un API muy agradable!")
    ```
    """

    def __init__(
        self, url_endpoint, token=None, chat_prompt_template=None, run_prompt_template=None, additional_tools=None
    ):
        self.url_endpoint = url_endpoint
        if token is None:
            self.token = f"Bearer {HfFolder().get_token()}"
        elif token.startswith("Bearer") or token.startswith("Basic"):
            self.token = token
        else:
            self.token = f"Bearer {token}"
        super().__init__(
            chat_prompt_template=chat_prompt_template,
            run_prompt_template=run_prompt_template,
            additional_tools=additional_tools,
        )

    def generate_one(self, prompt, stop):
        headers = {"Authorization": self.token}
        inputs = {
            "inputs": prompt,
            "parameters": {"max_new_tokens": 200, "return_full_text": False, "stop": stop},
        }

        response = requests.post(self.url_endpoint, json=inputs, headers=headers)
        if response.status_code == 429:
            logger.info("Getting rate-limited, waiting a tiny bit before trying again.")
            time.sleep(1)
            return self._generate_one(prompt)
        elif response.status_code != 200:
            raise ValueError(f"Error {response.status_code}: {response.json()}")

        result = response.json()[0]["generated_text"]
        # Inference API returns the stop sequence
        for stop_seq in stop:
            if result.endswith(stop_seq):
                return result[: -len(stop_seq)]
        return result


class LocalAgent(Agent):
    """
    Agent that uses a local model and tokenizer to generate code.

    Args:
        model ([`PreTrainedModel`]):
            The model to use for the agent.
        tokenizer ([`PreTrainedTokenizer`]):
            The tokenizer to use for the agent.
        chat_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `chat` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `chat_prompt_template.txt` in this repo in this case.
        run_prompt_template (`str`, *optional*):
            Pass along your own prompt if you want to override the default template for the `run` method. Can be the
            actual prompt template or a repo ID (on the Hugging Face Hub). The prompt should be in a file named
            `run_prompt_template.txt` in this repo in this case.
        additional_tools ([`Tool`], list of tools or dictionary with tool values, *optional*):
            Any additional tools to include on top of the default ones. If you pass along a tool with the same name as
            one of the default tools, that default tool will be overridden.

    Example:

    ```py
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, LocalAgent

    checkpoint = "bigcode/starcoder"
    model = AutoModelForCausalLM.from_pretrained(checkpoint, device_map="auto", torch_dtype=torch.bfloat16)
    tokenizer = AutoTokenizer.from_pretrained(checkpoint)

    agent = LocalAgent(model, tokenizer)
    agent.run("Draw me a picture of rivers and lakes.")
    ```
    """
    #这段代码主要定义了两个类：一个是LocalAgent，另一个是StopSequenceCriteria。下面我会逐行解释代码。
    #这是LocalAgent类的构造函数，接受五个参数。model和tokenizer是用于生成文本的模型和分词器。chat_prompt_template、run_prompt_template和additional_tools这三个参数是可选的，分别代表聊天提示模板、运行提示模板和附加工具。
    def __init__(self, model, tokenizer, chat_prompt_template=None, run_prompt_template=None, additional_tools=None):
        self.model = model
        self.tokenizer = tokenizer #这两行将输入的model和tokenizer保存为LocalAgent对象的属性。
        #调用父类的构造函数，传入chat_prompt_template、run_prompt_template和additional_tools。
        super().__init__(
            chat_prompt_template=chat_prompt_template,
            run_prompt_template=run_prompt_template,
            additional_tools=additional_tools,
        )
    
    
    @classmethod #这是一个修饰器，表示下面的方法是类方法。
    def from_pretrained(cls, pretrained_model_name_or_path, **kwargs):  #cls 是一个在类方法中被约定俗成地用来表示类本身的参数。
        """ #这是一个类方法，用于从预训练模型中创建LocalAgent对象。pretrained_model_name_or_path是预训练模型的名称或者路径，**kwargs是其他的关键字参数。
        Convenience method to build a `LocalAgent` from a pretrained checkpoint.

        Args:
            pretrained_model_name_or_path (`str` or `os.PathLike`):
                The name of a repo on the Hub or a local path to a folder containing both model and tokenizer.
            kwargs:
                Keyword arguments passed along to [`~PreTrainedModel.from_pretrained`].

        Example:

        ```py
        import torch
        from transformers import LocalAgent

        agent = LocalAgent.from_pretrained("bigcode/starcoder", device_map="auto", torch_dtype=torch.bfloat16)
        agent.run("Draw me a picture of rivers and lakes.")
        ```
        """
        model = AutoModelForCausalLM.from_pretrained(pretrained_model_name_or_path, **kwargs)
        tokenizer = AutoTokenizer.from_pretrained(pretrained_model_name_or_path, **kwargs)
        #cls(model, tokenizer) 就是在调用 LocalAgent 的构造函数（__init__），因为在这个上下文中，cls 就是 LocalAgent 类。
        #所以，return cls(model, tokenizer) 的作用就是创建一个新的 LocalAgent 实例，并返回这个实例。
        return cls(model, tokenizer)  #使用加载的模型和分词器创建LocalAgent对象，并返回。

    @property #是一个修饰器，表示下面的方法是一个属性。
    def _model_device(self): #这个方法返回模型的设备。
        if hasattr(self.model, "hf_device_map"):  #判断模型是否有hf_device_map属性。
            return list(self.model.hf_device_map.values())[0]  #果有hf_device_map属性，返回第一个设备。
        for param in self.mode.parameters():  #如果没有hf_device_map属性，遍历模型的参数。
            return param.device  #返回第一个参数的设备。

    def generate_one(self, prompt, stop):  #这个方法用于根据给定的提示生成一段文本。prompt是提示，stop是停止标志。
        encoded_inputs = self.tokenizer(prompt, return_tensors="pt").to(self._model_device)  #对提示进行编码，并将编码结果移动到模型的设备上。
        src_len = encoded_inputs["input_ids"].shape[1]  #获取输入的长度。
        stopping_criteria = StoppingCriteriaList([StopSequenceCriteria(stop, self.tokenizer)])  #创建停止条件，当生成的文本包含停止标志时停止生成。
        outputs = self.model.generate(  #生成文本
            encoded_inputs["input_ids"], max_new_tokens=200, stopping_criteria=stopping_criteria
        )

        result = self.tokenizer.decode(outputs[0].tolist()[src_len:])  #解码生成的文本。
        # Inference API returns the stop sequence
        for stop_seq in stop:  #遍历每一个停止序列。
            if result.endswith(stop_seq):
                result = result[: -len(stop_seq)]
        return result

#定义一个名为StopSequenceCriteria的类，它继承自StoppingCriteria。这个类用于在生成过程中遇到特定序列时停止生成。
class StopSequenceCriteria(StoppingCriteria):
    """
    This class can be used to stop generation whenever a sequence of tokens is encountered.

    Args:
        stop_sequences (`str` or `List[str]`):
            The sequence (or list of sequences) on which to stop execution.
        tokenizer:
            The tokenizer used to decode the model outputs.
    """
    #这是StopSequenceCriteria的构造函数，接受两个参数：停止序列和分词器。
    def __init__(self, stop_sequences, tokenizer):
        if isinstance(stop_sequences, str):  #如果stop_sequences是字符串，那么将其转化为列表。
            stop_sequences = [stop_sequences]
        self.stop_sequences = stop_sequences  #和 self.tokenizer = tokenizer 将输入的停止序列和分词器保存为StopSequenceCriteria对象的属性。
        self.tokenizer = tokenizer

    def __call__(self, input_ids, scores, **kwargs) -> bool:  #定义了该类的调用方法，输入参数为输入的id、得分以及其他关键字参数，返回值是布尔值。
        decoded_output = self.tokenizer.decode(input_ids.tolist()[0])  #将输入的id解码为文本。
        return any(decoded_output.endswith(stop_sequence) for stop_sequence in self.stop_sequences)   #如果解码出的文本以任何一个停止序列结尾，那么返回True，否则返回False。

'''
基础知识：
1.return cls(model, tokenizer) 这里面的cls是什么

在这段代码中，cls 是一个在类方法中被约定俗成地用来表示类本身的参数。

在 Python 中，实例方法（instance method）的第一个参数通常是 self，表示实例本身。类方法（class method）则不是在实例上调用，而是在类上调用。所以，类方法的第一个参数通常是 cls，表示类本身。

在这段特定的代码中，from_pretrained 是一个类方法，它加载一个预训练模型和一个预训练的分词器，然后使用这个模型和分词器来创建一个 LocalAgent 实例。cls(model, tokenizer) 就是在调用 LocalAgent 的构造函数（__init__），因为在这个上下文中，cls 就是 LocalAgent 类。

所以，return cls(model, tokenizer) 的作用就是创建一个新的 LocalAgent 实例，并返回这个实例。

2.isinstance() 是 Python 的一个内建函数，用于检查一个对象是否是某个类的实例，或者是否是某个类的子类的实例。

在这段代码中，isinstance(stop_sequences, str) 用来检查 stop_sequences 是否是 str 类（即字符串）的实例。如果 stop_sequences 是一个字符串，这个检查将返回 True；否则，返回 False。

这里的 if isinstance(stop_sequences, str): 判断 stop_sequences 是否为字符串类型。如果是字符串，那么将其封装成一个列表，因为后续的处理需要一个序列（例如列表）的停止序列。如果 stop_sequences 已经是一个列表（或其他序列类型），则不需要这个转换。

'''
