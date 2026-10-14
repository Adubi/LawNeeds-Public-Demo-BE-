def html(
    display_name,
    lawyer_name,
    consultation_reference,
    consultation_date,
    consultation_time,
    consultation_length,
    receipt_number,
    transaction_id,
    payment_date,
    subtotal_display,
    vat_amount_display,
    total_paid_display,
):
    return f"""
        <html>
        <head>
            <meta charset="utf-8" />
            <style>
                body {{ font-family: Arial, sans-serif; color: #1d1d1d; margin:0; padding:0 40px; background:#F0F5F2; }}
                .container {{ max-width:600px; margin:20px auto; background:#ffffff; border-radius:15px; overflow:hidden; }}
                .band {{ background:#033126; padding:44px 24px; text-align:center; }}
                .band h1 {{ margin:0; color:#ffffff; font-size:26px; line-height:1; }}
                .band .motto {{ margin:6px 0 0; color:#DD8550; font-size:13px; font-weight:600; letter-spacing:0.3px; }}
                .content {{ padding:28px 24px; }}
                .content p {{ line-height:1.5; font-size:15px; }}
                .section-title {{ font-size:15px; font-weight:bold; color:#033126; margin:24px 0 8px; }}
                .details-table {{ width:100%; border-collapse:collapse; }}
                .details-table td {{ padding:6px 0; font-size:14px; line-height:1.4; }}
                .details-table td.label {{ color:#5c5c5c; width:55%; }}
                .details-table td.value {{ color:#1d1d1d; text-align:right; font-weight:bold; }}
                .details-table tr.total td {{ border-top:1px solid #E5E5E5; padding-top:10px; font-size:15px; }}
                .callout {{ background:#F0F5F2; padding:16px 20px; border-radius:4px; border-left:4px solid #DD8550; margin-top:20px; }}
                .footer {{ background:#033126; padding:24px; text-align:center; }}
                .footer p {{ margin:10px 0; color:#ffffff; font-size:13px; }}
                .footer .tagline {{ font-weight:bold; font-size:15px; }}
                .footer a {{ color:#ffffff; text-decoration:underline; }}
                .copyright {{ margin-top:6px; font-size:11px; color:#B9B9B9; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="band">
                    <table role="presentation" class="band-inner" align="center" cellpadding="0" cellspacing="0" style="margin:0 auto;">
                        <tr>
                            <td valign="middle" align="right" style="padding:0 6px 4px 0; vertical-align:middle; line-height:0;">
                                <img src="https://lawneeds-official-db.s3.eu-west-2.amazonaws.com/email-assets/adaptive-icon.png" alt="LawNeeds" width="50" height="50" style="width:50px; height:50px; border-radius:8px; display:block; vertical-align:middle;" />
                            </td>
                            <td valign="middle" align="left" style="padding:0; vertical-align:middle;">
                                <h1 style="margin:0; color:#ffffff; font-size:26px; line-height:1;">LawNeeds</h1>
                                <p style="margin:6px 0 0; color:#DD8550; font-size:13px; font-weight:600; letter-spacing:0.3px;">Legal Support Made Simple</p>
                            </td>
                        </tr>
                    </table>
                </div>

                <div class="content">
                    <p>Hi <strong>{display_name}</strong>,</p>

                    <p>We've successfully received your consultation payment. Your consultation with <strong>{lawyer_name}</strong> has now been confirmed.</p>

                    <div class="section-title">Consultation Details</div>
                    <table class="details-table" role="presentation" cellpadding="0" cellspacing="0">
                        <tr><td class="label">Reference</td><td class="value">{consultation_reference}</td></tr>
                        <tr><td class="label">Date</td><td class="value">{consultation_date}</td></tr>
                        <tr><td class="label">Time</td><td class="value">{consultation_time}</td></tr>
                        <tr><td class="label">Duration</td><td class="value">{consultation_length}</td></tr>
                    </table>

                    <div class="callout">
                        <p style="margin:0;">You can access your consultation securely through your LawNeeds inbox at the scheduled time. Your receipt has been attached for your records.</p>
                    </div>

                    <div class="section-title">Payment Summary</div>
                    <table class="details-table" role="presentation" cellpadding="0" cellspacing="0">
                        <tr><td class="label">Receipt Number</td><td class="value">{receipt_number}</td></tr>
                        <tr><td class="label">Transaction ID</td><td class="value">{transaction_id}</td></tr>
                        <tr><td class="label">Payment Date</td><td class="value">{payment_date}</td></tr>
                        <tr><td class="label">Subtotal</td><td class="value">{subtotal_display}</td></tr>
                        <tr><td class="label">VAT (20%)</td><td class="value">{vat_amount_display}</td></tr>
                        <tr><td class="label">VAT Registration Number</td><td class="value">466727846</td></tr>
                        <tr class="total"><td class="label"><strong>Total Paid</strong></td><td class="value">{total_paid_display}</td></tr>
                    </table>

                    <p style="margin-top:24px;">Kind regards,<br/>The LawNeeds Team</p>
                </div>

                <div class="footer">
                    <p class="tagline">Legal Support Made Simple</p>
                    <p>Website: <a href="https://lawneeds.co.uk">lawneeds.co.uk</a></p>
                    <p>Support: <a href="mailto:support@lawneeds.co.uk">support@lawneeds.co.uk</a></p>
                    <p><a href="https://lawneeds.co.uk/privacy-policy-for-application/">Privacy Policy</a> | <a href="https://lawneeds.co.uk/term-and-conditions-users/">Terms of Use</a></p>
                    <p style="font-size:11px; color:#B9B9B9;">You're receiving this email because you paid for a consultation on LawNeeds. Manage notifications or unsubscribe in your account settings.</p>
                    <p class="copyright">&copy; 2026 LawNeeds Ltd. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """


def text(
    display_name,
    lawyer_name,
    consultation_reference,
    consultation_date,
    consultation_time,
    consultation_length,
    receipt_number,
    transaction_id,
    payment_date,
    subtotal_display,
    vat_amount_display,
    total_paid_display,
):
    return f"""
        Hi {display_name},

        We've successfully received your consultation payment. Your consultation with {lawyer_name} has now been confirmed.

        Consultation Details:
        Reference: {consultation_reference}
        Date: {consultation_date}
        Time: {consultation_time}
        Duration: {consultation_length}

        You can access your consultation securely through your LawNeeds inbox at the scheduled time. Your receipt has been attached for your records.

        Payment Summary:
        Receipt Number: {receipt_number}
        Transaction ID: {transaction_id}
        Payment Date: {payment_date}
        Subtotal: {subtotal_display}
        VAT (20%): {vat_amount_display}
        VAT Registration Number: 466727846
        Total Paid: {total_paid_display}

        Kind regards,
        The LawNeeds Team

        LawNeeds Ltd
        Legal Support Made Simple
        Website: https://lawneeds.co.uk
        Support: support@lawneeds.co.uk

        Privacy Policy: https://lawneeds.co.uk/privacy-policy-for-application/
        Terms of Use: https://lawneeds.co.uk/term-and-conditions-users/

        You're receiving this email because you paid for a consultation on LawNeeds.
        """