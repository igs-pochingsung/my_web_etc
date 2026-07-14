FROM python:3.12-alpine

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /code

# RUN ["python","OpenWebFlaskApp.py"]