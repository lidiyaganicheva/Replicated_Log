FROM python:3.9

WORKDIR /app
COPY . .

ADD ./requirements.txt .
RUN pip install -r requirements.txt


EXPOSE 9000
EXPOSE 9001
EXPOSE 9002


