import asyncio
import os
import sys

import httpx

# Add the project root to the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings

async def list_stores():
    settings = get_settings()
    api_key = settings.gemini_api_key
    
    if not api_key:
        print("Error: GEMINI_API_KEY is not set.")
        return

    print("--- Configured Stores (from settings) ---")
    print(f"Common Store: {settings.gemini_common_store_name}")
    print(f"Ticket Stores: {settings.gemini_ticket_store_names}")
    print("\n")

    print("--- Fetching Stores from Google API ---")
    
    # Try to list corpora (Semantic Retriever)
    # Note: The endpoint might vary depending on the exact feature used (Corpora vs File Service)
    # For "File Search" in Gemini 1.5, it might be using the File API or Corpora.
    # Let's try listing Corpora first.
    
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        # 1. List Corpora (Semantic Retriever)
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/corpora"
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                corpora = data.get("corpora", [])
                print(f"Found {len(corpora)} Corpora:")
                for corpus in corpora:
                    print(f"  - Name: {corpus.get('name')}")
                    print(f"    DisplayName: {corpus.get('displayName')}")
            else:
                print(f"Failed to list corpora: {response.status_code} {response.text}")
        except Exception as e:
            print(f"Error listing corpora: {e}")

        print("\n")
        
        # 2. List Files (File API) - used for File Search
        # https://ai.google.dev/api/files#v1beta.files.list
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/files"
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                files = data.get("files", [])
                print(f"Found {len(files)} Files (showing top 10):")
                for i, file in enumerate(files[:10]):
                    print(f"  - Name: {file.get('name')}")
                    print(f"    DisplayName: {file.get('displayName')}")
                    print(f"    MimeType: {file.get('mimeType')}")
            else:
                print(f"Failed to list files: {response.status_code} {response.text}")
        except Exception as e:
            print(f"Error listing files: {e}")

        print("\n")

        # 3. List File Search Stores (if endpoint exists)
        try:
            # This is a guess based on naming convention
            url = "https://generativelanguage.googleapis.com/v1beta/fileSearchStores" 
            # Or maybe it is not listable directly?
            # Let's try.
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                # The key might be fileSearchStores or similar
                stores = data.get("fileSearchStores", [])
                print(f"Found {len(stores)} File Search Stores:")
                for store in stores:
                    print(f"  - Name: {store.get('name')}")
            else:
                print(f"Failed to list fileSearchStores: {response.status_code} {response.text}")
        except Exception as e:
            print(f"Error listing fileSearchStores: {e}")

if __name__ == "__main__":
    asyncio.run(list_stores())
