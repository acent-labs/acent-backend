import asyncio
import os
import sys

import httpx

# Add the project root to the python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings

async def delete_corpus_documents(corpus_name: str, store_name: str):
    """Delete all documents in a corpus using fileSearchStore API, then delete the corpus itself."""
    settings = get_settings()
    api_key = settings.gemini_api_key
    
    if not api_key:
        print("Error: GEMINI_API_KEY is not set.")
        return

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. List all documents using fileSearchStore API
        try:
            # List documents using fileSearchStore endpoint
            url = f"https://generativelanguage.googleapis.com/v1beta/{store_name}/documents"
            print(f"Fetching documents from {store_name}...")
            response = await client.get(url, headers=headers)
            
            if response.status_code != 200:
                print(f"✗ Failed to list documents: {response.status_code}")
                print(f"Response: {response.text}")
                return
            
            data = response.json()
            documents = data.get("documents", [])
            print(f"Found {len(documents)} documents")
            
            # 2. For each document, delete its chunks first, then the document
            for doc in documents:
                doc_name = doc.get("name")
                if not doc_name:
                    continue
                    
                # List chunks in the document using corpus path
                # Try: corpora/{corpus}/documents/{doc}/chunks
                doc_id = doc_name.split("/")[-1]
                chunks_url = f"https://generativelanguage.googleapis.com/v1beta/{corpus_name}/documents/{doc_id}/chunks"
                print(f"  Fetching chunks from {corpus_name}/documents/{doc_id}...")
                chunks_response = await client.get(chunks_url, headers=headers)
                
                if chunks_response.status_code == 200:
                    chunks_data = chunks_response.json()
                    chunks = chunks_data.get("chunks", [])
                    print(f"  Found {len(chunks)} chunks")
                    
                    # Delete each chunk
                    for chunk in chunks:
                        chunk_name = chunk.get("name")
                        if not chunk_name:
                            continue
                        print(f"    Deleting chunk {chunk_name}...")
                        chunk_delete_url = f"https://generativelanguage.googleapis.com/v1beta/{chunk_name}"
                        chunk_delete_response = await client.delete(chunk_delete_url, headers=headers)
                        
                        if chunk_delete_response.status_code in [200, 204]:
                            print("    ✓ Deleted chunk")
                        else:
                            print(f"    ✗ Failed to delete chunk: {chunk_delete_response.status_code}")
                else:
                    print(f"  ✗ Failed to list chunks: {chunks_response.status_code}")
                
                # Now delete the document
                print(f"  Deleting document {doc_name}...")
                delete_url = f"https://generativelanguage.googleapis.com/v1beta/{doc_name}"
                delete_response = await client.delete(delete_url, headers=headers)
                
                if delete_response.status_code in [200, 204]:
                    print(f"  ✓ Deleted {doc_name}")
                else:
                    print(f"  ✗ Failed to delete {doc_name}: {delete_response.status_code}")
                    print(f"    Response: {delete_response.text}")
            
            # 3. Delete the corpus
            print(f"Deleting corpus {corpus_name}...")
            corpus_url = f"https://generativelanguage.googleapis.com/v1beta/{corpus_name}"
            corpus_response = await client.delete(corpus_url, headers=headers)
            
            if corpus_response.status_code in [200, 204]:
                print(f"✓ Successfully deleted {corpus_name}")
            else:
                print(f"✗ Failed to delete {corpus_name}")
                print(f"Status: {corpus_response.status_code}")
                print(f"Response: {corpus_response.text}")
                
        except Exception as e:
            print(f"✗ Error: {e}")

async def main():
    if len(sys.argv) < 2:
        print("Usage: python delete_gemini_corpus_with_docs.py <store_name>")
        print("Example: python delete_gemini_corpus_with_docs.py fileSearchStores/freshworks-kb-with-metadata-azp7fvki3ejh")
        sys.exit(1)
    
    store_name = sys.argv[1]
    
    # Extract corpus name from fileSearchStore name
    if store_name.startswith("fileSearchStores/"):
        corpus_id = store_name.replace("fileSearchStores/", "")
        corpus_name = f"corpora/{corpus_id}"
    else:
        corpus_name = store_name
        store_name = f"fileSearchStores/{store_name.replace('corpora/', '')}"
    
    await delete_corpus_documents(corpus_name, store_name)

if __name__ == "__main__":
    asyncio.run(main())
