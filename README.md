# tranformers-code-explained--line-by-line

转载至Hugging Face transformer项目官方，是个人的理解编辑加工加上chatgpt4给出的代码解读版本。尽量做到逐行解释，小白友好。
欢迎大家提交pr，和star、fork，提交自己的源码理解。

注：xxxx表示伪目录，非有效。

* [/src](./src)
   * [xx.py](./src/utils/common.py)
   * [xx.py](./src/utils/peft_trainer.py) 
   * [/src/transformers](/src/transformers)
     * [training_args.py（do_train、do_eval）](/src/transformers/training_args.py)
     * [trainer_seq2seq.py](/src/transformers/trainer_seq2seq.py)
       * [/src/transformers/tools/]
         * [agents.py（.from_pretrained()）](/src/transformers/tools/agents.py)
  * [xx.py](./src/train_sft.py)
* [xx/](./examples)
  * [xx.md](./examples/ads_generation.md)
* [README.md](./README.md)

  


# CSDN彩色博客版：
* [xxxx/](./ChatGLM-Efficient-Tuning-Explained/src)
  * [xxxx/](./ChatGLM-Efficient-Tuning-Explained/src/utils)
    * [xxxx.py](./ChatGLM-Efficient-Tuning-Explained/src/utils/common.py)
    * [xxxx.py](./ChatGLM-Efficient-Tuning-Explained/src/utils/peft_trainer.py)
      * [CSDN彩色博客版/src/transformers/tools/agents.py（.from_pretrained）：agents.py](https://zengxiaojian.blog.csdn.net/article/details/131578327)
* [README.md](./ChatGLM-Efficient-Tuning-Explained/README.md)


