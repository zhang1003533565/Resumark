import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.routers import resumes as resumes_router
from app.schemas.models import ImproveResumeRequest


class TestImprovePreviewFallback(unittest.IsolatedAsyncioTestCase):
    async def test_preview_returns_local_fallback_when_ai_flow_fails(self) -> None:
        resume_data = {
            "personalInfo": {
                "name": "张三",
                "title": "后端工程师",
                "email": "zhangsan@example.com",
            },
            "summary": "负责后端服务开发。",
            "workExperience": [
                {
                    "title": "后端工程师",
                    "company": "示例科技",
                    "years": "2022 - Present",
                    "description": ["维护 FastAPI 服务"],
                }
            ],
            "education": [],
            "personalProjects": [],
            "additional": {"technicalSkills": ["Python", "FastAPI"]},
        }
        resume = {
            "resume_id": "resume_1",
            "content": json.dumps(resume_data, ensure_ascii=False),
            "content_type": "json",
            "processed_data": copy.deepcopy(resume_data),
        }
        job = {
            "job_id": "job_1",
            "content": "Senior Python role building FastAPI services with Docker.",
        }

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = resume
        mock_db.get_job.return_value = job
        mock_db.update_job.return_value = {**job, "preview_hash": "hash"}

        request = ImproveResumeRequest(resume_id="resume_1", job_id="job_1")

        with (
            patch.object(resumes_router, "db", mock_db),
            patch.object(
                resumes_router,
                "_improve_preview_flow",
                AsyncMock(side_effect=RuntimeError("LLM unavailable")),
            ),
        ):
            response = await resumes_router.improve_resume_preview_endpoint(request)

        self.assertIsNone(response.data.resume_id)
        self.assertEqual(response.data.resume_preview.personalInfo.name, "张三")
        self.assertIn("AI 定制暂时不可用", response.data.warnings[0])
        self.assertIn("本地预览兜底", response.data.warnings[1])
        self.assertTrue(response.data.improvements)
        self.assertIn("突出", response.data.improvements[0].suggestion)
        mock_db.update_job.assert_awaited()
