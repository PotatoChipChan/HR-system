"""app/recruitment/contract_pdf.py – Generate employment contract PDF"""
import os
from datetime import datetime
from flask import current_app
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                 TableStyle, HRFlowable, Image)
from app.database import query


def generate_contract_pdf(contract_id, company_id=None):
    contract = query("""
        SELECT c.*, ja.applicant_name, ja.applicant_email, ja.application_id,
               jp.title as job_title, COALESCE(d.department_name, '') as department_name
        FROM Contract c
        JOIN Job_Application ja ON c.application_id = ja.application_id
        LEFT JOIN Job_Posting jp ON ja.posting_id = jp.posting_id
        LEFT JOIN Department d ON c.department_id = d.department_id
        WHERE c.contract_id = ?
    """, (contract_id,), one=True)
    if not contract:
        return None

    # Load company info and signature
    company = {'name': 'SmartHR Sdn Bhd', 'signature_path': None, 'address': ''}
    if company_id:
        c = query("SELECT name, signature_path, address FROM Company WHERE company_id=?",
                  (company_id,), one=True)
        if c:
            company['name'] = c['name']
            company['signature_path'] = c['signature_path']
            company['address'] = c['address'] or ''
    company_name = company['name']
    signature_img = None
    if company['signature_path']:
        sig_path = os.path.join(current_app.root_path, '..', 'uploads', 'signatures',
                                company['signature_path'])
        if os.path.exists(sig_path):
            try:
                signature_img = Image(sig_path, width=120, height=40)
            except Exception:
                signature_img = None

    # Null-safe defaults
    salary = contract['base_salary'] or 0
    offer_date = contract['offer_date'] or datetime.now().strftime('%Y-%m-%d')
    start_date = contract['start_date'] or offer_date

    upload_dir = os.path.join(current_app.root_path, '..', 'uploads', 'contracts')
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"contract_{contract_id}.pdf"
    filepath = os.path.join(upload_dir, filename)

    doc = SimpleDocTemplate(filepath, pagesize=A4,
                            topMargin=20*mm, bottomMargin=20*mm,
                            leftMargin=20*mm, rightMargin=20*mm)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('Title2', parent=styles['Title'],
                                  fontSize=18, spaceAfter=4*mm,
                                  alignment=TA_CENTER, textColor=colors.HexColor('#1a6b3a'))
    heading_style = ParagraphStyle('Heading2', parent=styles['Heading2'],
                                    fontSize=13, spaceAfter=3*mm,
                                    spaceBefore=5*mm, textColor=colors.HexColor('#1e293b'))
    body_style = ParagraphStyle('Body2', parent=styles['Normal'],
                                 fontSize=10, leading=15, spaceAfter=2*mm)
    bold_style = ParagraphStyle('BoldBody', parent=body_style, fontSize=10, leading=15)
    clause_style = ParagraphStyle('Clause', parent=body_style, fontSize=10,
                                   leading=15, leftIndent=8*mm)
    signature_style = ParagraphStyle('Signature', parent=body_style,
                                      fontSize=11, spaceBefore=6*mm)
    footer_style = ParagraphStyle('Footer', parent=body_style, fontSize=8,
                                   textColor=colors.grey, alignment=TA_CENTER)

    total_emp = query(
        "SELECT COUNT(*) as c FROM Employee WHERE company_id=? AND employment_status='Active'",
        (company_id or 0,), one=True)['c']

    elements = []

    elements.append(Paragraph(company_name.upper(), title_style))
    elements.append(Paragraph("Employment Contract", ParagraphStyle(
        'SubTitle', parent=title_style, fontSize=14, textColor=colors.HexColor('#475569'))))
    elements.append(Spacer(1, 4*mm))

    line = HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#cbd5e1'))
    elements.append(line)
    elements.append(Spacer(1, 4*mm))

    elements.append(Paragraph("THIS EMPLOYMENT CONTRACT is made on "
                    f"{datetime.strptime(offer_date, '%Y-%m-%d').strftime('%d %B %Y')}", body_style))
    elements.append(Spacer(1, 2*mm))
    elements.append(Paragraph("<b>BETWEEN:</b>", bold_style))
    elements.append(Paragraph(
        f"{company_name} having its registered address at "
        f"{company['address'] or '__________________'}"
        "(hereinafter referred to as the \"<b>Employer</b>\")", clause_style))
    elements.append(Spacer(1, 2*mm))
    elements.append(Paragraph("<b>AND:</b>", bold_style))
    elements.append(Paragraph(
        f"<b>{contract['applicant_name']}</b> (NRIC No: ______________) of [Address] "
        f"(hereinafter referred to as the \"<b>Employee</b>\")", clause_style))
    elements.append(Spacer(1, 2*mm))
    elements.append(Paragraph(
        "<b>WHEREAS</b> the Employer has agreed to employ the Employee and the Employee "
        "has agreed to serve the Employer on the terms and conditions hereinafter set forth.",
        body_style))
    elements.append(Spacer(1, 3*mm))

    elements.append(Paragraph("NOW IT IS HEREBY AGREED AS FOLLOWS:", body_style))
    elements.append(Spacer(1, 3*mm))

    clauses = [
        ("1. APPOINTMENT",
         f"The Employer agrees to employ the Employee and the Employee agrees to serve "
         f"the Employer in the capacity of <b>{contract['position']}</b> within the "
         f"<b>{contract['department_name']}</b> department. The Employee shall report to "
         f"the Head of Department or such other person as the Employer may designate from "
         f"time to time."),

        ("2. COMMENCEMENT DATE",
         f"The Employee's employment shall commence on "
         f"<b>{datetime.strptime(start_date, '%Y-%m-%d').strftime('%d %B %Y')}</b> "
         f"(hereinafter referred to as the \"Commencement Date\")."),

        ("3. PROBATION PERIOD",
         "The Employee shall serve a probation period of three (3) months commencing from "
         "the Commencement Date. Upon completion of the probation period, the Employer may, "
         "at its sole discretion, confirm the Employee's employment in writing. The Employer "
         "reserves the right to extend the probation period if the Employee's performance is "
         "deemed unsatisfactory."),

        ("4. REMUNERATION",
         f"The Employer shall pay the Employee a monthly salary of "
         f"<b>RM {salary:,.2f}</b> (\"Basic Salary\"), payable on the last "
         f"day of each calendar month. The Basic Salary shall be subject to statutory "
         f"deductions including but not limited to SOCSO (PERKESO), EPF (KWSP), EIS (SIP), "
         f"and PCB (income tax) as required by Malaysian law."),

        ("5. WORKING HOURS",
         f"The Employee's official working hours shall be from "
         f"<b>{contract['work_start_time']}</b> to <b>{contract['work_end_time']}</b>, "
         f"Monday through Friday, with a one (1) hour lunch break. The Employee may be "
         f"required to work reasonable additional hours as necessary for the proper "
         f"discharge of their duties."),

        ("6. EMPLOYMENT TYPE",
         f"This is a <b>{contract['employment_type']}</b> position. {'The Employee shall be entitled to the benefits and privileges applicable to full-time employees of the Employer.' if contract['employment_type'] == 'Full-Time' else 'The terms and conditions specific to this employment type shall apply as per company policy.'}"),

        ("7. LEAVE ENTITLEMENT",
         "The Employee shall be entitled to the following leave benefits in accordance "
         "with the Employment Act 1955 and the Employer's policies:"
         "<br/>&nbsp;&nbsp;&nbsp;&nbsp;(a) Annual leave: Twelve (12) working days per annum, "
         "pro-rated for incomplete years of service."
         "<br/>&nbsp;&nbsp;&nbsp;&nbsp;(b) Medical leave: Fourteen (14) days per annum, "
         "with hospitalization leave up to sixty (60) days."
         "<br/>&nbsp;&nbsp;&nbsp;&nbsp;(c) Public holidays: All gazetted public holidays "
         "in Malaysia as observed by the Employer."),

        ("8. TERMINATION",
         "This contract may be terminated by either party giving thirty (30) days' written "
         "notice to the other party. The Employer reserves the right to terminate the "
         "Employee's employment without notice in cases of gross misconduct, dishonesty, "
         "or any other act constituting just cause for summary dismissal under Malaysian law. "
         "Upon termination, the Employee shall return all company property and documents "
         "in their possession."),

        ("9. CONFIDENTIALITY",
         "The Employee shall not, during or after the term of employment, disclose or "
         "make use of any confidential information concerning the Employer's business, "
         "trade secrets, client data, financial information, or any other proprietary "
         "matters. This obligation shall survive the termination of this contract."),

        ("10. NON-COMPETE",
         "For a period of six (6) months following the termination of employment, the "
         "Employee shall not, directly or indirectly, engage in or render services to any "
         "business that competes with the Employer's operations within Malaysia."),

        ("11. INTELLECTUAL PROPERTY",
         "All intellectual property rights arising from the Employee's work during the "
         "course of employment shall vest solely in the Employer. The Employee agrees to "
         "execute all documents necessary to perfect the Employer's rights therein."),

        ("12. COMPANY POLICIES",
         "The Employee shall at all times comply with the Employer's internal policies, "
         "procedures, and codes of conduct as amended from time to time. The Employee "
         "acknowledges receipt of the Employee Handbook and agrees to be bound by its "
         "provisions."),

        ("13. MEDICAL EXAMINATION",
         "The Employer reserves the right to require the Employee to undergo a medical "
         "examination at the Employer's expense. Continued employment may be subject to "
         "satisfactory medical clearance."),

        ("14. GOVERNING LAW",
         "This contract shall be governed by and construed in accordance with the laws of "
         "Malaysia. Any disputes arising from this contract shall be subject to the "
         "exclusive jurisdiction of the courts of Malaysia."),
    ]

    for title, text in clauses:
        elements.append(Paragraph(title, heading_style))
        elements.append(Paragraph(text, clause_style))
        elements.append(Spacer(1, 1.5*mm))

    elements.append(Spacer(1, 6*mm))
    elements.append(line)

    elements.append(Spacer(1, 4*mm))
    elements.append(Paragraph("ACCEPTANCE", heading_style))
    elements.append(Paragraph(
        "The Employee hereby acknowledges and agrees to the terms and conditions set "
        "out in this Employment Contract. The Employee acknowledges having read and "
        "understood each clause and voluntarily accepts this offer of employment.",
        body_style))
    elements.append(Spacer(1, 6*mm))

    today = datetime.now().strftime('%d %B %Y')
    # Employer column — show signature image if available, else blank line
    if signature_img:
        employer_sig = [
            Paragraph("<br/>", signature_style),
            signature_img,
            Paragraph(f"<br/>{company_name}<br/>Date: {today}", signature_style),
        ]
    else:
        employer_sig = Paragraph(
            "<br/><br/>___________________________<br/>Name: _______________<br/>Date: _______________",
            signature_style)
    sig_data = [
        [Paragraph("<b>SIGNED by the EMPLOYER:</b>", signature_style),
         Paragraph("<b>SIGNED by the EMPLOYEE:</b>", signature_style)],
        [employer_sig,
         Paragraph(f"<br/><br/>___________________________<br/>Name: {contract['applicant_name']}<br/>Date: _______________", signature_style)],
    ]
    sig_table = Table(sig_data, colWidths=[doc.width/2.0]*2)
    sig_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(sig_table)

    elements.append(Spacer(1, 10*mm))
    elements.append(Paragraph(
        f"Contract Reference: CTR-{contract_id:04d} | Generated: {today} | "
        f"{company_name}", footer_style))

    doc.build(elements)
    return filepath
