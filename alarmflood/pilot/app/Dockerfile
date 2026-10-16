FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
# 若云服务器访问 pypi 慢，改成：
# RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py /app/app.py
EXPOSE 8000
CMD ["python", "-u", "/app/app.py"]
