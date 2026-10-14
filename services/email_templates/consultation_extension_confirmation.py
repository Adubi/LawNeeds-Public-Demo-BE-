def html(
    display_name,
    lawyer_name,
    additional_minutes,
    new_consultation_end_time,
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
                .callout {{ background:#F0F5F2; padding:16px 20px; border-radius:4px; border-left:4px solid #DD8550; margin-top:20px; }}
                .security-callout {{ background:#F0F5F2; padding:16px 20px; border-radius:4px; border-left:4px solid #DD8550; margin-top:24px; }}
                .security-callout p {{ margin:0; font-size:13px; line-height:1.5; }}
                .security-callout .label {{ display:block; color:#033126; font-weight:bold; margin-bottom:6px; font-size:13px; }}
                .payment-table {{ width:100%; border-collapse:collapse; margin-top:20px; }}
                .payment-table td {{ padding:8px 0; font-size:14px; border-bottom:1px solid #EDEDED; }}
                .payment-table td.label {{ color:#5b5b5b; }}
                .payment-table td.value {{ text-align:right; color:#1d1d1d; }}
                .payment-table tr.total td {{ border-bottom:none; padding-top:14px; font-weight:bold; font-size:16px; color:#033126; }}
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

                    <p>Your consultation extension payment has been processed successfully.</p>

                    <p>Your consultation with <strong>{lawyer_name}</strong> has now been extended by <strong>{additional_minutes}</strong>. Your updated consultation will now conclude at <strong>{new_consultation_end_time}</strong>.</p>

                    <div class="callout">
                        <p style="margin:0;">A receipt has been attached for your records.</p>
                    </div>

                    <table class="payment-table" role="presentation" cellpadding="0" cellspacing="0">
                        <tr>
                            <td class="label">Receipt Number</td>
                            <td class="value">{receipt_number}</td>
                        </tr>
                        <tr>
                            <td class="label">Transaction ID</td>
                            <td class="value">{transaction_id}</td>
                        </tr>
                        <tr>
                            <td class="label">Payment Date</td>
                            <td class="value">{payment_date}</td>
                        </tr>
                        <tr>
                            <td class="label">Subtotal</td>
                            <td class="value">{subtotal_display}</td>
                        </tr>
                        <tr>
                            <td class="label">VAT (20%)</td>
                            <td class="value">{vat_amount_display}</td>
                        </tr>
                        <tr>
                            <td class="label">VAT Registration Number</td>
                            <td class="value">466727846</td>
                        </tr>
                        <tr class="total">
                            <td>Total Paid</td>
                            <td class="value">{total_paid_display}</td>
                        </tr>
                    </table>

                    <div class="security-callout">
                        <span class="label">Security Notice</span>
                        <p>LawNeeds will never ask you to provide your password or full payment card details by email. If you receive a suspicious email claiming to be from us, please contact <a href="mailto:support@lawneeds.co.uk" style="color:#033126;">support@lawneeds.co.uk</a> before taking any action.</p>
                    </div>

                    <p style="margin-top:20px;">Kind regards,<br/>The LawNeeds Team</p>
                </div>

                <div class="footer">
                    <p class="tagline">Legal Support Made Simple</p>
                    <p>Website: <a href="https://lawneeds.co.uk">lawneeds.co.uk</a></p>
                    <p>Support: <a href="mailto:support@lawneeds.co.uk">support@lawneeds.co.uk</a></p>
                    <p><a href="https://lawneeds.co.uk/privacy-policy-for-application/">Privacy Policy</a> | <a href="https://lawneeds.co.uk/term-and-conditions-users/">Terms of Use</a></p>
                    <p style="font-size:11px; color:#B9B9B9;">You're receiving this email because you made a payment on your LawNeeds account.</p>
                    <p class="copyright">&copy; 2026 LawNeeds Ltd. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """


def text(
    display_name,
    lawyer_name,
    additional_minutes,
    new_consultation_end_time,
    receipt_number,
    transaction_id,
    payment_date,
    subtotal_display,
    vat_amount_display,
    total_paid_display,
):
    return f"""
        Hi {display_name},

        Your consultation extension payment has been processed successfully.

        Your consultation with {lawyer_name} has now been extended by {additional_minutes}. Your updated consultation will now conclude at {new_consultation_end_time}.

        A receipt has been attached for your records.

        Payment Summary:
        Receipt Number: {receipt_number}
        Transaction ID: {transaction_id}
        Payment Date: {payment_date}
        Subtotal: {subtotal_display}
        VAT (20%): {vat_amount_display}
        VAT Registration Number: 466727846
        Total Paid: {total_paid_display}

        Security Notice: LawNeeds will never ask you to provide your password or full payment card details by email. If you receive a suspicious email claiming to be from us, please contact support@lawneeds.co.uk before taking any action.

        Kind regards,
        The LawNeeds Team

        LawNeeds Ltd
        Legal Support Made Simple
        Website: https://lawneeds.co.uk
        Support: support@lawneeds.co.uk

        Privacy Policy: https://lawneeds.co.uk/privacy-policy-for-application/
        Terms of Use: https://lawneeds.co.uk/term-and-conditions-users/

        You're receiving this email because you made a payment on your LawNeeds account.
        """