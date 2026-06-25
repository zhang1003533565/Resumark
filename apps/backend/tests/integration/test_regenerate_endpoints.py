import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import enrichment as enrichment_router
from app.schemas.enrichment import RegenerateItemInput, RegenerateRequest, RegeneratedItem


class TestRegenerateSchemas(unittest.TestCase):
    def test_regenerate_request_instruction_max_length(self) -> None:
        item = RegenerateItemInput(
            item_id="skills",
            item_type="skills",
            title="Skills",
            current_content=["Python"],
        )

        RegenerateRequest(
            resume_id="resume_1",
            items=[item],
            instruction="x" * 2000,
            output_language="en",
        )

        with self.assertRaises(ValidationError):
            RegenerateRequest(
                resume_id="resume_1",
                items=[item],
                instruction="x" * 2001,
                output_language="en",
            )


class TestEnrichmentAnalyzeEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_falls_back_when_llm_fails(self) -> None:
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
                "personalProjects": [
                    {
                        "name": "简历工具",
                        "role": "开发者",
                        "description": [],
                    }
                ],
            }
        }

        with (
            patch.object(enrichment_router, "db", mock_db),
            patch.object(
                enrichment_router,
                "complete_json",
                AsyncMock(side_effect=RuntimeError("LLM unavailable")),
            ),
        ):
            response = await enrichment_router.analyze_resume("resume_1")

        self.assertEqual([item.item_id for item in response.items_to_enrich], ["exp_0", "proj_0"])
        self.assertEqual([question.item_id for question in response.questions], ["exp_0", "proj_0"])
        self.assertIn("AI 分析暂时不可用", response.analysis_summary or "")
        self.assertIn("请补充", response.questions[0].question)


class TestRegenerateEndpoints(unittest.IsolatedAsyncioTestCase):
    async def test_regenerate_processes_multiple_items_in_parallel(self) -> None:
        resume_id = "resume_1"
        request = RegenerateRequest(
            resume_id=resume_id,
            items=[
                RegenerateItemInput(
                    item_id="exp_0",
                    item_type="experience",
                    title="Senior Software Engineer",
                    subtitle="Google",
                    current_content=["Old"],
                ),
                RegenerateItemInput(
                    item_id="skills",
                    item_type="skills",
                    title="Technical Skills",
                    current_content=["Python"],
                ),
            ],
            instruction="Improve wording",
            output_language="en",
        )

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {"processed_data": {"workExperience": [], "additional": {}}}

        exp_item = RegeneratedItem(
            item_id="exp_0",
            item_type="experience",
            title="Senior Software Engineer",
            subtitle="Google",
            original_content=["Old"],
            new_content=["New"],
            diff_summary="Summary",
        )
        skills_item = RegeneratedItem(
            item_id="skills",
            item_type="skills",
            title="Technical Skills",
            original_content=["Python"],
            new_content=["Python", "TypeScript"],
            diff_summary="Summary",
        )

        with (
            patch.object(enrichment_router, "db", mock_db),
            patch.object(
                enrichment_router,
                "_regenerate_experience_or_project",
                AsyncMock(return_value=exp_item),
            ) as mock_regenerate_item,
            patch.object(
                enrichment_router,
                "_regenerate_skills",
                AsyncMock(return_value=skills_item),
            ) as mock_regenerate_skills,
        ):
            response = await enrichment_router.regenerate_items(request)

        self.assertEqual(
            [item.item_id for item in response.regenerated_items],
            ["exp_0", "skills"],
        )
        mock_regenerate_item.assert_awaited()
        mock_regenerate_skills.assert_awaited()

    async def test_regenerate_falls_back_for_failed_items(self) -> None:
        resume_id = "resume_1"
        request = RegenerateRequest(
            resume_id=resume_id,
            items=[
                RegenerateItemInput(
                    item_id="exp_0",
                    item_type="experience",
                    title="Senior Software Engineer",
                    subtitle="Google",
                    current_content=["Old"],
                ),
                RegenerateItemInput(
                    item_id="skills",
                    item_type="skills",
                    title="Technical Skills",
                    current_content=["Python"],
                ),
            ],
            instruction="Improve wording",
            output_language="en",
        )

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {"processed_data": {"workExperience": [], "additional": {}}}

        skills_item = RegeneratedItem(
            item_id="skills",
            item_type="skills",
            title="Technical Skills",
            original_content=["Python"],
            new_content=["Python", "TypeScript"],
            diff_summary="Summary",
        )

        with (
            patch.object(enrichment_router, "db", mock_db),
            patch.object(
                enrichment_router,
                "_regenerate_experience_or_project",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(
                enrichment_router,
                "_regenerate_skills",
                AsyncMock(return_value=skills_item),
            ),
        ):
            response = await enrichment_router.regenerate_items(request)

        self.assertEqual(
            [item.item_id for item in response.regenerated_items],
            ["exp_0", "skills"],
        )
        self.assertEqual(response.errors, [])
        fallback_item = response.regenerated_items[0]
        self.assertIn("AI 重新生成暂时不可用", fallback_item.diff_summary)
        self.assertTrue(fallback_item.new_content)

    async def test_regenerate_returns_fallback_when_all_items_fail(self) -> None:
        resume_id = "resume_1"
        request = RegenerateRequest(
            resume_id=resume_id,
            items=[
                RegenerateItemInput(
                    item_id="exp_0",
                    item_type="experience",
                    title="运营实习生",
                    subtitle="示例公司",
                    current_content=["负责日常运营"],
                ),
                RegenerateItemInput(
                    item_id="skills",
                    item_type="skills",
                    title="技能",
                    current_content=["Python", "Python"],
                ),
            ],
            instruction="写得更有结果感",
            output_language="zh",
        )

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {"processed_data": {"workExperience": [], "additional": {}}}

        with (
            patch.object(enrichment_router, "db", mock_db),
            patch.object(
                enrichment_router,
                "_regenerate_experience_or_project",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(
                enrichment_router,
                "_regenerate_skills",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            response = await enrichment_router.regenerate_items(request)

        self.assertEqual(
            [item.item_id for item in response.regenerated_items],
            ["exp_0", "skills"],
        )
        self.assertEqual(response.errors, [])
        self.assertIn("优化并突出结果", response.regenerated_items[0].new_content[0])
        self.assertEqual(response.regenerated_items[1].new_content, ["Python"])

    async def test_apply_regenerated_falls_back_to_metadata_matching(self) -> None:
        resume_id = "resume_1"
        processed_data = {
            "workExperience": [
                {
                    "title": "Some Other Role",
                    "company": "OtherCo",
                    "description": ["Keep me"],
                },
                {
                    "title": "Senior Software Engineer",
                    "company": "Google",
                    "description": ["Old bullet"],
                },
            ],
            "personalProjects": [],
            "additional": {"technicalSkills": ["Python"]},
        }

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {"processed_data": processed_data}
        mock_db.update_resume.return_value = None

        regenerated_items = [
            RegeneratedItem(
                item_id="exp_0",  # stale index: exp_0 no longer points to the matching entry
                item_type="experience",
                title="Senior Software Engineer",
                subtitle="Google",
                original_content=["Old bullet"],
                new_content=["New bullet"],
                diff_summary="Summary",
            )
        ]

        with patch.object(enrichment_router, "db", mock_db):
            result = await enrichment_router.apply_regenerated_items(resume_id, regenerated_items)

        self.assertEqual(result["updated_items"], 1)

        update_payload = mock_db.update_resume.call_args.args[1]
        updated = update_payload["processed_data"]

        self.assertEqual(updated["workExperience"][0]["description"], ["Keep me"])
        self.assertEqual(updated["workExperience"][1]["description"], ["New bullet"])

    async def test_apply_regenerated_disambiguates_duplicates_by_original_content(self) -> None:
        resume_id = "resume_1"
        processed_data = {
            "workExperience": [
                {"title": "Engineer", "company": "Google", "description": ["Bullet A"]},
                {"title": "Engineer", "company": "Google", "description": ["Bullet B"]},
            ],
            "personalProjects": [],
            "additional": {"technicalSkills": ["Python"]},
        }

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {"processed_data": processed_data}
        mock_db.update_resume.return_value = None

        regenerated_items = [
            RegeneratedItem(
                item_id="exp_0",  # could point to a different duplicate after reordering
                item_type="experience",
                title="Engineer",
                subtitle="Google",
                original_content=["Bullet B"],
                new_content=["Bullet B (rewritten)"],
                diff_summary="Summary",
            )
        ]

        with patch.object(enrichment_router, "db", mock_db):
            result = await enrichment_router.apply_regenerated_items(resume_id, regenerated_items)

        self.assertEqual(result["updated_items"], 1)

        updated = mock_db.update_resume.call_args.args[1]["processed_data"]
        self.assertEqual(updated["workExperience"][0]["description"], ["Bullet A"])
        self.assertEqual(updated["workExperience"][1]["description"], ["Bullet B (rewritten)"])

    async def test_apply_regenerated_refuses_when_items_do_not_match(self) -> None:
        resume_id = "resume_1"
        processed_data = {
            "workExperience": [
                {"title": "Engineer", "company": "Acme", "description": ["Old"]},
            ],
            "personalProjects": [],
            "additional": {"technicalSkills": ["Python"]},
        }

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {"processed_data": processed_data}

        regenerated_items = [
            RegeneratedItem(
                item_id="exp_0",
                item_type="experience",
                title="Different Title",
                subtitle="Different Co",
                original_content=["Old"],
                new_content=["New"],
                diff_summary="Summary",
            )
        ]

        with patch.object(enrichment_router, "db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                await enrichment_router.apply_regenerated_items(resume_id, regenerated_items)

        self.assertEqual(ctx.exception.status_code, 409)
        mock_db.update_resume.assert_not_called()

    async def test_apply_regenerated_updates_skills_for_additional_and_legacy_paths(self) -> None:
        resume_id = "resume_1"

        base_item = RegeneratedItem(
            item_id="skills",
            item_type="skills",
            title="Technical Skills",
            original_content=["Python"],
            new_content=["Python", "TypeScript"],
            diff_summary="Summary",
        )

        # additional.technicalSkills path
        mock_db_additional = AsyncMock()
        mock_db_additional.get_resume.return_value = {
            "processed_data": {"additional": {"technicalSkills": ["Python"]}}
        }
        mock_db_additional.update_resume.return_value = None

        with patch.object(enrichment_router, "db", mock_db_additional):
            result = await enrichment_router.apply_regenerated_items(resume_id, [base_item])

        self.assertEqual(result["updated_items"], 1)
        updated = mock_db_additional.update_resume.call_args.args[1]["processed_data"]
        self.assertEqual(updated["additional"]["technicalSkills"], ["Python", "TypeScript"])

        # legacy technicalSkills path
        mock_db_legacy = AsyncMock()
        mock_db_legacy.get_resume.return_value = {"processed_data": {"technicalSkills": ["Python"]}}
        mock_db_legacy.update_resume.return_value = None

        with patch.object(enrichment_router, "db", mock_db_legacy):
            result = await enrichment_router.apply_regenerated_items(resume_id, [base_item])

        self.assertEqual(result["updated_items"], 1)
        updated = mock_db_legacy.update_resume.call_args.args[1]["processed_data"]
        self.assertEqual(updated["technicalSkills"], ["Python", "TypeScript"])

    async def test_apply_regenerated_skills_fails_when_no_supported_path_exists(self) -> None:
        resume_id = "resume_1"

        mock_db = AsyncMock()
        mock_db.get_resume.return_value = {"processed_data": {"workExperience": []}}

        regenerated_items = [
            RegeneratedItem(
                item_id="skills",
                item_type="skills",
                title="Technical Skills",
                original_content=["Python"],
                new_content=["Python", "TypeScript"],
                diff_summary="Summary",
            )
        ]

        with patch.object(enrichment_router, "db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                await enrichment_router.apply_regenerated_items(resume_id, regenerated_items)

        self.assertEqual(ctx.exception.status_code, 409)
        mock_db.update_resume.assert_not_called()
