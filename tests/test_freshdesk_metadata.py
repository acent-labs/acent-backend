import asyncio


from app.services.freshdesk_metadata import FreshdeskMetadataService


class StubClient:
    async def get_ticket_fields(self):
        return [
            {
                "name": "status",
                "choices": [
                    {"value": 2, "label": "Open"},
                    {"value": 5, "label": "Closed"},
                ],
            },
            {
                "name": "priority",
                "choices": [
                    {"value": 3, "label": "High"},
                    {"value": 4, "label": "Urgent"},
                ],
            },
        ]

    async def get_categories(self):
        return [
            {"id": 10, "name": "FAQ"},
        ]

    async def get_folders(self, category_id: int):
        return [
            {"id": 99, "name": "General", "category_id": category_id},
        ]


def service_with_stub():
    return FreshdeskMetadataService(ttl_hours=0, client=StubClient())


def test_freshdesk_metadata_resolves_priority():
    service = service_with_stub()
    priority = asyncio.run(service.resolve_priority_label("High"))
    assert priority == 3


def test_freshdesk_metadata_resolves_status():
    service = service_with_stub()
    status = asyncio.run(service.resolve_status_label("Closed"))
    assert status == 5


def test_freshdesk_metadata_resolves_category_and_folder():
    service = service_with_stub()
    category_id = asyncio.run(service.resolve_category_id("FAQ"))
    assert category_id == 10
    folder_id = asyncio.run(service.resolve_folder_id("General", category_id))
    assert folder_id == 99
