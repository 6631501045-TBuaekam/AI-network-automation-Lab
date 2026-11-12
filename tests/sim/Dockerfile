FROM python:3.12-slim
RUN pip install --no-cache-dir paramiko==3.5.1
WORKDIR /sim
COPY fake_ios.py /sim/fake_ios.py
COPY sim/run_sim.py /sim/sim/run_sim.py
EXPOSE 2201 2202
CMD ["python", "-u", "/sim/sim/run_sim.py"]
