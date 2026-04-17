#!/bin/bash
pip install -r requirements.txt
uvicorn idith.app:app --host 0.0.0.0 --port 8000
