
import requests
import json
import time

url = "http://localhost:8000/api/ai/chat"
query = "Find 10 qualified clinical trial sites in Europe that are seeing at least 40 newly diagnosed paediatric patients per year and that have maximum 1 ongoing clinical trial for patients above 12 years."

start_time = time.time()
print(f"Sending query: {query}")
try:
    response = requests.post(url, json={"messages": [{"role": "user", "content": query}], "stream": False}, timeout=60)
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
except Exception as e:
    print(f"Error: {e}")
print(f"Time taken: {time.time() - start_time:.2f}s")
