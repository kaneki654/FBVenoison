import requests
prompt = "hello"
url = f"https://text.pollinations.ai/{prompt}"
params = {
    "model": "openai",
    "json": "false"
}
response = requests.get(url, params=params)
if response.status_code == 200:
    print(response.text)
else:
    print(f"Error: {response.status_code}")