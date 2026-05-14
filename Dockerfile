FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple

# 复制代码
COPY . .

# 创建数据目录
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python", "app.py"]
