FROM yamanetoshi/qark-base:0.9

ADD fabfile.py /tmp/
ADD config.py /tmp/
RUN chmod a+x /tmp/fabfile.py
RUN pip install fabric
RUN pip install boto
WORKDIR /tmp

ENTRYPOINT [ "/usr/local/bin/fab", "setup" ]