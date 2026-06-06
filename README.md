# LabelDataInformationCollect (LDIC)

标注信息收集系统

## 项目背景
学校里开展了视频标注任务，之前一直都是使用共享表格进行数据填报，结果经常出现数据不一致、共享表格吞数据、其他同学偷数据~~以及在共享表格里写情书~~的情况，为了提高标注效率和数据准确性，我决定开发一个标注信息收集系统。

## 项目介绍
本系统是一个标注信息收集系统，主要功能包括：
- 标注员可填写标注任务名、视频时长信息，并保存到数据库中。
- 管理员可查看所有所有标注记录。
- 支持Excel导出

## 部署
1. 克隆本仓库：
```bash
git clone https://github.com/Tydecent/LDIC.git
```

2. 进入项目目录：
```bash
cd ./LDIC/
```

3. 创建并激活虚拟环境(可选)：

    **创建**：
    ```bash
    python -m venv .venv
    ```
    **激活**：
    Linux:
    ```bash
    ./.venv/bin/activate
    ```
    Windows:
    ```powershell
    .venv\Scripts\activate
    ```

4. 安装依赖：
```bash
pip install -r requirements.txt
```

5. 启动服务器：
```bash
python server.py
```