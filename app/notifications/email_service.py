"""app/notifications/email_service.py – Email notification sender"""
import os
import re
import mimetypes
from flask import render_template
from flask_mail import Message
from app import mail
from app.database import query, log_audit

TEMPLATE_MAP = {
    'Leave Application Approved': 'emails/leave_approved.html',
    'Leave Application Rejected': 'emails/leave_rejected.html',
    'Invoice Approved': 'emails/invoice_approved.html',
    'Invoice Rejected': 'emails/invoice_rejected.html',
    'IC Access Request': 'emails/ic_access_requested.html',
    'IC Request Approved': 'emails/ic_access_result.html',
    'IC Request Rejected': 'emails/ic_access_result.html',
    'Payslip Ready': 'emails/payslip_ready.html',
    'New Attendance Request': 'emails/attendance_request.html',
    'Attendance Request Approved': 'emails/attendance_request.html',
    'Attendance Request Rejected': 'emails/attendance_request.html',
    'Face Recognition System Error': 'emails/face_system_alert.html',
    'Face Recognition System Restored': 'emails/face_system_alert.html',
}


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip()


def send_email(subject, recipient, html_body, attachments=None):
    try:
        msg = Message(subject=subject, recipients=[recipient])
        msg.body = strip_html(html_body)
        msg.html = html_body
        if attachments:
            for filepath in attachments:
                with open(filepath, 'rb') as f:
                    fname = os.path.basename(filepath)
                    ctype, _ = mimetypes.guess_type(filepath)
                    msg.attach(fname, ctype or 'application/octet-stream', f.read())
        mail.send(msg)
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send to {recipient}: {e}")
        return False


def send_email_notification(employee_id, title, message, extra_context=None):
    emp = query("SELECT full_name, email FROM Employee WHERE employee_id=?", (employee_id,), one=True)
    if not emp or not emp['email']:
        return

    template = TEMPLATE_MAP.get(title)
    if not template:
        return

    ctx = dict(
        employee_name=emp['full_name'],
        title=title,
        message=message,
    )
    if extra_context:
        ctx.update(extra_context)

    try:
        html_body = render_template(template, **ctx)
    except Exception as e:
        print(f"[EMAIL] Template render failed for {template}: {e}")
        log_audit('SEND_EMAIL', 'Notifications', f'Template render failed: {template}', action_status='Failed')
        return

    success = send_email(f'SmartHR - {title}', emp['email'], html_body)
    log_audit('SEND_EMAIL', 'Notifications',
              f'Email {"sent" if success else "failed"} to {emp["email"]}: {title}',
              action_status='Success' if success else 'Failed')
