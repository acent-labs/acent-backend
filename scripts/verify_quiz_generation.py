import asyncio
import sys
import os
from uuid import UUID
import logging

# Configure logging to see what's happening
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add current directory to sys.path
sys.path.append(os.getcwd())

from app.api.routes.curriculum import get_questions  # noqa: E402

async def main():
    module_id = UUID('dce2d97e-bedf-47b7-91cc-8c6d96c21b44')
    print(f"Testing quiz generation for module: {module_id}")

    try:
        # This calls the endpoint function directly
        # It should trigger the logic:
        # 1. Fetch module (verify name_ko fix)
        # 2. Fetch content (empty)
        # 3. Trigger RAG fallback (verify RAG search)
        # 4. Generate questions via Gemini
        questions = await get_questions(module_id=module_id)
        
        print(f"\n✅ Success! Generated {len(questions)} questions:")
        for i, q in enumerate(questions, 1):
            print(f"{i}. {q.question}")
            print(f"   (Choices: {', '.join([c.text for c in q.choices])})")
            
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
