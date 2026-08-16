"""Shared visibility rules for recruitment application queries."""

from app.database import query


_COMPANY_RECRUITMENT_ROLES = ('Admin', 'HR', 'HR Manager')


def application_visibility_scope(user_session, *, application_alias='ja',
                                 posting_alias='jp', branch_alias='b'):
    """Return the SQL condition and parameters for visible applications."""
    role = user_session.get('user_role')
    managed_dept_id = user_session.get('managed_dept_id')

    if user_session.get('is_dept_manager') and managed_dept_id:
        return f'{posting_alias}.department_id=?', [managed_dept_id]
    if role == 'Manager':
        return f'{posting_alias}.branch_id=?', [user_session.get('branch_id')]
    if role in _COMPANY_RECRUITMENT_ROLES:
        company_scope = (
            f'({application_alias}.company_id=? OR {branch_alias}.company_id=?)'
        )
        company_id = user_session.get('company_id')
        if role in ('Admin', 'HR Manager'):
            return (
                f'({company_scope} OR '
                f'({application_alias}.company_id IS NULL '
                f'AND {application_alias}.posting_id IS NULL))',
                [company_id, company_id],
            )
        return company_scope, [company_id, company_id]
    return '1=0', []


def count_visible_new_applications(user_session):
    """Count New applications visible to the supplied authenticated session."""
    scope_sql, scope_params = application_visibility_scope(user_session)
    row = query(f"""
        SELECT COUNT(*) AS c
        FROM Job_Application ja
        LEFT JOIN Job_Posting jp ON ja.posting_id=jp.posting_id
        LEFT JOIN Branch b ON jp.branch_id=b.branch_id
        WHERE ja.status='New' AND ({scope_sql})
    """, scope_params, one=True)
    return row['c'] if row else 0
