"""AI-powered application scorer using multi-factor analysis."""
import re, os
from nltk.corpus import stopwords
from nltk.stem import PorterStemmer

STOP_WORDS = set(stopwords.words('english'))
STEMMER = PorterStemmer()

# Broad skill keywords commonly found in resumes
SKILL_KEYWORDS = {
    'python', 'java', 'javascript', 'typescript', 'c++', 'c#', 'ruby', 'php', 'go', 'rust',
    'swift', 'kotlin', 'scala', 'r', 'matlab', 'sql', 'nosql', 'mongodb', 'postgresql',
    'mysql', 'redis', 'elasticsearch', 'aws', 'azure', 'gcp', 'docker', 'kubernetes',
    'jenkins', 'git', 'linux', 'agile', 'scrum', 'jira', 'react', 'angular', 'vue',
    'node.js', 'django', 'flask', 'spring', 'laravel', 'rails', 'tensorflow', 'pytorch',
    'machine learning', 'deep learning', 'nlp', 'computer vision', 'data science',
    'data analysis', 'data engineering', 'business intelligence', 'tableau', 'power bi',
    'excel', 'sap', 'oracle', 'salesforce', 'project management', 'leadership',
    'communication', 'teamwork', 'problem solving', 'critical thinking', 'analytical',
    'customer service', 'sales', 'marketing', 'accounting', 'finance', 'hr', 'recruiting',
    'supply chain', 'logistics', 'operations', 'strategy', 'consulting', 'negotiation',
    'public speaking', 'technical writing', 'ui/ux', 'product management', 'qa',
    'testing', 'automation', 'devops', 'cicd', 'rest api', 'graphql', 'microservices',
}


def _extract_resume_text(resume_path, app_root):
    """Extract text from a resume file (PDF or plain text)."""
    if not resume_path:
        return ''
    full_path = os.path.join(app_root, '..', 'uploads', 'resumes', resume_path)
    full_path = os.path.normpath(full_path)
    if not os.path.exists(full_path):
        return ''
    ext = os.path.splitext(full_path)[1].lower()
    try:
        if ext == '.pdf':
            import pdfplumber
            with pdfplumber.open(full_path) as pdf:
                return ' '.join(page.extract_text() or '' for page in pdf.pages)
        elif ext in ('.txt', '.text'):
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        elif ext == '.docx':
            try:
                import docx
                doc = docx.Document(full_path)
                return ' '.join(p.text for p in doc.paragraphs)
            except ImportError:
                pass
    except Exception as e:
        print(f"[SCORER] Resume extraction failed for {resume_path}: {e}")
    return ''


def _preprocess(text):
    text = re.sub(r'[^a-zA-Z\s]', ' ', text.lower())
    words = [STEMMER.stem(w) for w in text.split() if w not in STOP_WORDS and len(w) > 2]
    return ' '.join(words)


def _detect_skills(text):
    """Return set of recognised skill keywords found in text."""
    lower = text.lower()
    found = set()
    for skill in SKILL_KEYWORDS:
        if skill in lower:
            found.add(skill)
    return found


def score_applications(posting, applications, app_root=None):
    """Score applications using multi-factor analysis.

    Factors:
      - Keyword coverage against posting (50%)
      - Relevant skills detected (30%)
      - Content depth / detail (20%)

    Returns list of dicts sorted by score descending.
    """
    posting_text = ' '.join(filter(None, [
        posting.get('title') or '',
        posting.get('description') or '',
        posting.get('requirements') or '',
    ]))

    if not posting_text.strip():
        return [{
            'application_id': a['application_id'],
            'applicant_name': a['applicant_name'],
            'score': 0,
            'summary': 'No job requirements to match against.',
            'is_shortlisted': False,
        } for a in applications]

    posting_keywords = set(_preprocess(posting_text).split())
    posting_skills = _detect_skills(posting_text)

    results = []
    for a in applications:
        # Build full candidate text from cover_letter + resume
        parts = [a.get('cover_letter') or '']
        resume_text = _extract_resume_text(a.get('resume_path'), app_root)
        if resume_text:
            parts.append(resume_text)
        text = ' '.join(filter(None, parts))
        if not text.strip():
            text = a.get('applicant_name') or ''

        app_keywords = set(_preprocess(text).split())
        app_skills = _detect_skills(text)

        # Factor 1: Keyword coverage (50% weight)
        if posting_keywords:
            matched_keywords = posting_keywords & app_keywords
            keyword_score = len(matched_keywords) / len(posting_keywords)
        else:
            matched_keywords = set()
            keyword_score = 0

        # Factor 2: Skills relevance (30% weight)
        if posting_skills:
            matched_skills = posting_skills & app_skills
            skill_score = len(matched_skills) / len(posting_skills)
        else:
            matched_skills = set()
            skill_score = 0.5  # neutral if posting has no listed skills

        # Factor 3: Content depth (20% weight)
        raw_len = len(text.strip())
        depth_score = min(1.0, raw_len / 2000)

        # Composite score
        raw = (keyword_score * 50) + (skill_score * 30) + (depth_score * 20)
        score = min(100, round(raw, 1))

        # Build summary
        top_terms = sorted(matched_keywords, key=lambda w: len(w), reverse=True)[:5]
        summary_parts = []
        if score > 60:
            summary_parts.append('Strong match')
        elif score >= 30:
            summary_parts.append('Moderate match')
        else:
            summary_parts.append('Weak match')
        if top_terms:
            summary_parts.append(f"Keywords: {', '.join(top_terms)}")
        if matched_skills:
            shown = sorted(matched_skills)[:5]
            summary_parts.append(f"Skills: {', '.join(shown)}")
        summary = '. '.join(summary_parts) if summary_parts else 'No significant match found.'
        is_shortlisted = score > 60

        results.append({
            'application_id': a['application_id'],
            'applicant_name': a['applicant_name'],
            'score': score,
            'summary': summary,
            'is_shortlisted': is_shortlisted,
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results
