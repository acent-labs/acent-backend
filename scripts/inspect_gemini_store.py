import asyncio
import os
import sys

import httpx

# Add the project root to the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings

async def inspect_store(store_name: str):
    """Inspect the structure of a fileSearchStore."""
    settings = get_settings()
    api_key = settings.gemini_api_key
    
    if not api_key:
        print("Error: GEMINI_API_KEY is not set.")
        return

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    # Extract corpus name
    if store_name.startswith("fileSearchStores/"):
        corpus_id = store_name.replace("fileSearchStores/", "")
        corpus_name = f"corpora/{corpus_id}"
    else:
        corpus_name = store_name
        store_name = f"fileSearchStores/{store_name.replace('corpora/', '')}"
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get corpus info
        print(f"=== Inspecting {corpus_name} ===\n")
        
        try:
            corpus_url = f"https://generativelanguage.googleapis.com/v1beta/{corpus_name}"
            print(f"1. GET {corpus_url}")
            response = await client.get(corpus_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                print(f"   Status: {response.status_code}")
                print(f"   Name: {data.get('name')}")
                print(f"   DisplayName: {data.get('displayName')}")
                print(f"   CreateTime: {data.get('createTime')}")
                print(f"   UpdateTime: {data.get('updateTime')}")
            else:
                print(f"   Status: {response.status_code}")
                print(f"   Response: {response.text}")
        except Exception as e:
            print(f"   Error: {e}")
        
        print()
        
        # List documents
        try:
            docs_url = f"https://generativelanguage.googleapis.com/v1beta/{store_name}/documents"
            print(f"2. GET {docs_url}")
            response = await client.get(docs_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                documents = data.get("documents", [])
                print(f"   Status: {response.status_code}")
                print(f"   Found {len(documents)} documents:")
                for doc in documents:
                    print(f"   - {doc.get('name')}")
                    print(f"     State: {doc.get('state')}")
                    print(f"     CreateTime: {doc.get('createTime')}")
            else:
                print(f"   Status: {response.status_code}")
                print(f"   Response: {response.text}")
        except Exception as e:
            print(f"   Error: {e}")
        
        print()
        
        # Try alternate document path
        try:
            docs_url_alt = f"https://generativelanguage.googleapis.com/v1beta/{corpus_name}/documents"
            print(f"3. GET {docs_url_alt} (alternate path)")
            response = await client.get(docs_url_alt, headers=headers)
            print(f"   Status: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                documents = data.get("documents", [])
                print(f"   Found {len(documents)} documents:")
                for doc in documents:
                    print(f"   - {doc.get('name')}")
            else:
                print(f"   Response: {response.text[:200]}")
        except Exception as e:
            print(f"   Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python inspect_gemini_store.py <store_name>")
        print("Example: python inspect_gemini_store.py fileSearchStores/freshworks-kb-with-metadata-azp7fvki3ejh")
        sys.exit(1)
    
    store_name = sys.argv[1]
    asyncio.run(inspect_store(store_name))
