# The embedder (app.py): a small multilingual text-embedding model on the
# CPU. Its own image, so the app image carries neither ONNX Runtime nor the
# model. Same Python as the app image.
FROM python:3.13-slim

# HF_HUB_DISABLE_XET: the model comes over plain HTTPS. Hugging Face's
# "xet" transfer stalled at 0 bytes when this was tested.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    EMBED_CACHE_DIR=/models \
    HF_HOME=/models/.hf \
    HF_HUB_DISABLE_XET=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && useradd --system --uid 10001 --no-create-home --home-dir /models embedder \
    && mkdir /models && chown embedder /models

COPY app.py .
USER embedder
# The model files go into the image now, fetched and loaded once as the user
# that serves them: the container starts without the internet and always
# runs the same model.
RUN python app.py --download
# And never asks Hugging Face for anything at run time.
ENV HF_HUB_OFFLINE=1

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
