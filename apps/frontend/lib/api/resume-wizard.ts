import type { ResumeData } from '@/components/dashboard/resume-component';
import { apiPost } from './client';

export type ResumeWizardSection =
  | 'intro'
  | 'contact'
  | 'summary'
  | 'workExperience'
  | 'internships'
  | 'education'
  | 'personalProjects'
  | 'skills'
  | 'review';

export type ResumeWizardStep = 'intro' | 'question' | 'review' | 'complete';
export type ResumeWizardAction = 'start' | 'answer' | 'skip' | 'back' | 'review';

export interface ResumeWizardQuestion {
  text: string;
  section: ResumeWizardSection;
}

export interface ResumeWizardProgress {
  current: number;
  total: number;
}

export interface ResumeWizardHistoryEntry {
  question: string;
  answer: string;
  section: ResumeWizardSection;
  resume_data_before: ResumeData;
}

export interface ResumeWizardState {
  step: ResumeWizardStep;
  resume_data: ResumeData;
  current_question: ResumeWizardQuestion;
  history: ResumeWizardHistoryEntry[];
  asked_count: number;
  inferred_skills: string[];
  is_complete: boolean;
  progress: ResumeWizardProgress;
  warnings: string[];
}

export interface ResumeWizardTurnRequest {
  state: ResumeWizardState;
  action: ResumeWizardAction;
  answer?: { text: string };
}

export interface ResumeWizardTurnResponse {
  state: ResumeWizardState;
}

export interface ResumeWizardFinalizeResponse {
  message: string;
  request_id: string;
  resume_id: string;
  processing_status: 'ready';
  is_master: boolean;
}

export const INTRO_QUESTION =
  '你好，我会帮你一步步建立主简历。先告诉我你的姓名，以及你想投递或发展的职位方向。';

export const SECTION_QUESTIONS: Record<ResumeWizardSection, string> = {
  intro: INTRO_QUESTION,
  contact: '你希望简历里放哪些联系方式？可以写邮箱、电话、城市、LinkedIn、GitHub 或个人网站。',
  summary: '用一两句话描述你的职业定位、优势或目标方向。',
  workExperience: '请介绍一段工作经历：职位、公司、时间、你负责什么，以及有什么可量化成果。',
  internships: '请介绍一段实习经历：职位、公司、时间、你做了什么，以及带来了什么结果。',
  education: '请介绍你的教育背景：学校、专业/学位、时间，以及荣誉、课程或亮点。',
  personalProjects: '请介绍一个项目：你做了什么、为什么重要、用了哪些技术，以及最终结果。',
  skills: '你希望简历里展示哪些工具、技术或能力？',
  review: '我们先检查现有内容，再创建你的主简历。',
};

function emptyResumeData(): ResumeData {
  return {
    personalInfo: {
      name: '',
      title: '',
      email: '',
      phone: '',
      location: '',
      website: '',
      linkedin: '',
      github: '',
    },
    summary: '',
    workExperience: [],
    education: [],
    personalProjects: [],
    additional: { technicalSkills: [], languages: [], certificationsTraining: [], awards: [] },
    customSections: {},
    sectionMeta: [],
  };
}

export function createInitialResumeWizardState(): ResumeWizardState {
  return {
    step: 'intro',
    resume_data: emptyResumeData(),
    current_question: { text: INTRO_QUESTION, section: 'intro' },
    history: [],
    asked_count: 0,
    inferred_skills: [],
    is_complete: false,
    progress: { current: 0, total: 8 },
    warnings: [],
  };
}

export async function postResumeWizardTurn(
  payload: ResumeWizardTurnRequest
): Promise<ResumeWizardTurnResponse> {
  const response = await apiPost('/resume-wizard/turn', payload);
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(text || `Resume wizard turn failed with status ${response.status}`);
  }
  return response.json();
}

export async function finalizeResumeWizard(
  state: ResumeWizardState
): Promise<ResumeWizardFinalizeResponse> {
  const response = await apiPost('/resume-wizard/finalize', { state });
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(text || `Resume wizard finalize failed with status ${response.status}`);
  }
  return response.json();
}
