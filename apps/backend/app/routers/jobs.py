"""Job description management endpoints."""

from fastapi import APIRouter, HTTPException

from app.database import db
from app.schemas import JobUploadRequest, JobUploadResponse

router = APIRouter(prefix="/jobs", tags=["职位描述"])


@router.post("/upload", response_model=JobUploadResponse)
async def upload_job_descriptions(request: JobUploadRequest) -> JobUploadResponse:
    """Upload one or more job descriptions.

    Stores the raw text for later use in resume tailoring.
    Returns an array of job_ids corresponding to the input array.
    """
    if not request.job_descriptions:
        raise HTTPException(status_code=400, detail="请先填写职位描述。")

    job_ids = []
    for jd in request.job_descriptions:
        if not jd.strip():
            raise HTTPException(status_code=400, detail="职位描述不能为空。")

        job = await db.create_job(
            content=jd.strip(),
            resume_id=request.resume_id,
        )
        job_ids.append(job["job_id"])

    return JobUploadResponse(
        message="职位描述已处理",
        job_id=job_ids,
        request={
            "job_descriptions": request.job_descriptions,
            "resume_id": request.resume_id,
        },
    )


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    """Get job description by ID."""
    job = await db.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="未找到职位描述。")

    return job
