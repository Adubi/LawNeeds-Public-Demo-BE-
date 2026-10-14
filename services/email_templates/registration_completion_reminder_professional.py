def html(display_name, base_url):
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
                .features {{ margin-top:16px; padding-left:20px; }}
                .features li {{ margin-bottom:8px; line-height:1.4; }}
                .cta-wrap {{ text-align:center; margin-top:24px; }}
                .cta-button {{ display:inline-block; background:#DD8550; color:#ffffff !important; text-decoration:none; font-weight:600; font-size:15px; padding:14px 32px; border-radius:6px; }}
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

                    <p>You're almost set up with LawNeeds.</p>

                    <p>It looks like you started creating your professional account but didn't complete the registration process.</p>

                    <p>Complete your account to access:</p>
                    <ul class="features">
                        <li>Referrals from clients seeking verified UK immigration support</li>
                        <li>A dedicated professional profile visible to those who need your expertise</li>
                        <li>Support through text or voice (supporting 60 languages currently)</li>
                        <li>Document and image uploads for contextual case guidance</li>
                    </ul>

                    <div class="cta-wrap">
                        <a href="{base_url}" class="cta-button">Complete Your Registration</a>
                    </div>

                    <p style="margin-top:20px;">It only takes a few minutes to finish setting up your account and continue where you left off.</p>

                    <p>If you've already completed your registration, please disregard this email.</p>

                    <p>Kind regards,<br/>The LawNeeds Team</p>
                </div>

                <div class="footer">
                    <p class="tagline">Legal Support Made Simple</p>
                    <p>Website: <a href="https://lawneeds.co.uk">lawneeds.co.uk</a></p>
                    <p>Support: <a href="mailto:support@lawneeds.co.uk">support@lawneeds.co.uk</a></p>
                    <p><a href="https://lawneeds.co.uk/privacy-policy-for-application/">Privacy Policy</a> | <a href="https://lawneeds.co.uk/term-and-conditions-users/">Terms of Use</a></p>
                    <p style="font-size:11px; color:#B9B9B9;">You're receiving this email because you started creating a LawNeeds professional account. Manage notifications or unsubscribe in your account settings.</p>
                    <p class="copyright">&copy; 2026 LawNeeds Ltd. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """


def text(display_name, base_url):
    return f"""
        Hi {display_name},

        You're almost set up with LawNeeds.

        It looks like you started creating your professional account but didn't complete the registration process.

        Complete your account to access:
        - Referrals from clients seeking verified UK immigration support
        - A dedicated professional profile visible to those who need your expertise
        - Support through text or voice (supporting 60 languages currently)
        - Document and image uploads for contextual case guidance

        Complete Your Registration: {base_url}

        It only takes a few minutes to finish setting up your account and continue where you left off.

        If you've already completed your registration, please disregard this email.

        Kind regards,
        The LawNeeds Team

        LawNeeds Ltd
        Legal Support Made Simple
        Website: https://lawneeds.co.uk
        Support: support@lawneeds.co.uk

        Privacy Policy: https://lawneeds.co.uk/privacy-policy-for-application/
        Terms of Use: https://lawneeds.co.uk/term-and-conditions-users/

        You're receiving this email because you started creating a LawNeeds professional account.
        """