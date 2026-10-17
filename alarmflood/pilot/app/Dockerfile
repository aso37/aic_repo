FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
# 国内服务器默认走清华源（pypi.org 经常超时导致构建失败）
# 若服务器在境外，删掉 -i 之后的部分即可
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || pip install --no-cache-dir -r requirements.txt
COPY app.py /app/app.py
EXPOSE 8000
CMD ["python", "-u", "/app/app.py"]
