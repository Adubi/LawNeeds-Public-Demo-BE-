def html(display_name, subscription_name, renewal_date, renewal_amount):
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
                .details {{ background:#F0F5F2; border-radius:6px; padding:18px 20px; margin-top:16px; }}
                .details table {{ width:100%; border-collapse:collapse; }}
                .details td {{ padding:6px 0; font-size:14px; }}
                .details td.label {{ color:#5c5c5c; }}
                .details td.value {{ text-align:right; font-weight:bold; color:#033126; }}
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

                    <p>Just a reminder that your <strong>{subscription_name}</strong> subscription is due to renew in 3 days.</p>

                    <div class="details">
                        <table role="presentation">
                            <tr>
                                <td class="label">Plan</td>
                                <td class="value">{subscription_name}</td>
                            </tr>
                            <tr>
                                <td class="label">Renewal Date</td>
                                <td class="value">{renewal_date}</td>
                            </tr>
                            <tr>
                                <td class="label">Renewal Amount</td>
                                <td class="value">£{renewal_amount}</td>
                            </tr>
                        </table>
                    </div>

                    <p style="margin-top:20px;">Your subscription will renew automatically using your existing payment method, allowing you to continue accessing the features and allowances included in your plan without interruption.</p>

                    <div class="callout">
                        <p style="margin:0;">If you'd like to make changes to your subscription or cancel your upcoming renewal, you can manage your subscription through the LawNeeds app.</p>
                    </div>

                    <p style="margin-top:20px;">No action is required if you'd like your subscription to continue.</p>

                    <p>Thank you for using LawNeeds.</p>

                    <p>Kind regards,<br/>The LawNeeds Team</p>
                </div>

                <div class="footer">
                    <p class="tagline">Legal Support Made Simple</p>
                    <p>Website: <a href="https://lawneeds.co.uk">lawneeds.co.uk</a></p>
                    <p>Support: <a href="mailto:support@lawneeds.co.uk">support@lawneeds.co.uk</a></p>
                    <p><a href="https://lawneeds.co.uk/privacy-policy-for-application/">Privacy Policy</a> | <a href="https://lawneeds.co.uk/term-and-conditions-users/">Terms of Use</a></p>
                    <p style="font-size:11px; color:#B9B9B9;">You're receiving this email because you have an active LawNeeds subscription. Manage notifications or unsubscribe in your account settings.</p>
                    <p class="copyright">&copy; 2026 LawNeeds Ltd. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """


def text(display_name, subscription_name, renewal_date, renewal_amount):
    return f"""
        Hi {display_name},

        Just a reminder that your {subscription_name} subscription is due to renew in 3 days.

        Subscription Details
        Plan: {subscription_name}
        Renewal Date: {renewal_date}
        Renewal Amount: £{renewal_amount}

        Your subscription will renew automatically using your existing payment method, allowing you to continue accessing the features and allowances included in your plan without interruption.

        If you'd like to make changes to your subscription or cancel your upcoming renewal, you can manage your subscription through the LawNeeds app.

        No action is required if you'd like your subscription to continue.

        Thank you for using LawNeeds.

        Kind regards,
        The LawNeeds Team

        LawNeeds Ltd
        Legal Support Made Simple
        Website: https://lawneeds.co.uk
        Support: support@lawneeds.co.uk

        Privacy Policy: https://lawneeds.co.uk/privacy-policy-for-application/
        Terms of Use: https://lawneeds.co.uk/term-and-conditions-users/

        You're receiving this email because you have an active LawNeeds subscription.
        """