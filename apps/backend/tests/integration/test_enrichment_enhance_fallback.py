import unittest
from unittest.mock import AsyncMock, patch

from app.routers import enrichment as enrichment_router
from app.schemas.enrichment import AnswerInput, EnhanceRequest


class TestEnrichmentEnhanceFallback(unittest.IsolatedAsyncioTestCase):
    async def test_enhance_uses_local_fallback_when_ai_is_unavailable(self) -> None:
        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {
            "processed_data": {
                "workExperience": [
                    {
                        "title": "运营实习生",
                        "company": "示例公司",
                        "description": ["负责日常运营"],
                    }
                ],
                "personalProjects": [],
            }
        }

        request = EnhanceRequest(
            resume_id="resume_1",
            answers=[
                AnswerInput(
                    question_id="q_0",
                    answer="把日报处理时间从 2 小时缩短到 30 分钟。",
                )
            ],
        )

        with (
            patch.object(enrichment_router, "db", mock_db),
            patch.object(
                enrichment_router,
                "complete_json",
                AsyncMock(side_effect=RuntimeError("LLM unavailable")),
            ),
        ):
            response = await enrichment_router.generate_enhancements(request)

        self.assertEqual(len(response.enhancements), 1)
        enhancement = response.enhancements[0]
        self.assertEqual(enhancement.item_id, "exp_0")
        self.assertIn("运营实习生", enhancement.enhanced_description[0])
        self.assertIn("30 分钟", enhancement.enhanced_description[0])
