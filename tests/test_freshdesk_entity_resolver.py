import asyncio

from app.services.freshdesk_entity_resolver import FreshdeskEntityResolver


class StubClient:
    async def search_contacts(self, term):
        return {
            "results": [
                {"id": 1, "name": "Alice", "email": "alice@example.com"},
            ]
        }

    async def search_agents(self, term):
        return {
            "results": [
                {"id": 2, "name": "Agent Bob", "email": "bob@example.com"},
            ]
        }


def test_entity_resolver_returns_matches():
    resolver = FreshdeskEntityResolver(client=StubClient())
    result = asyncio.run(resolver.resolve("alice"))
    assert result.clarification_needed is True
    assert result.matches[0].name == "Alice"
    assert result.matches[1].type == "agent"


def test_entity_resolver_no_term():
    resolver = FreshdeskEntityResolver(client=StubClient())
    result = asyncio.run(resolver.resolve(""))
    assert result.clarification_needed is True
    assert result.matches == []
