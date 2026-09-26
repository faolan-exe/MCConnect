import queue
import smtplib
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from colorlogx import get_logger

logger = get_logger("smtp")


class SMTPMailer:
    """Sends emails from a background thread over one reused STARTTLS connection."""

    IDLE_TIMEOUT = 5     # seconds without mails before the connection is closed
    SEND_INTERVAL = 0.8  # pause between mails (provider rate limits)

    def __init__(self, smtp_server, smtp_port, username, password, sender=None):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.sender = sender or username
        self.email_queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self._smtp_worker, name="smtp-worker", daemon=True)
        self.worker_thread.start()

    def send_email(self, recipient, subject, html):
        """Queue an email; it is sent in the background."""
        logger.info(f"Queueing email to {recipient}")
        self.email_queue.put((recipient, subject, html))

    def stop(self):
        """Send the queued mails and stop the worker."""
        self.email_queue.put(None)
        self.worker_thread.join()

    def _connect(self):
        server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15)
        server.starttls()
        server.login(self.username, self.password)
        return server

    def _smtp_worker(self):
        server = None
        while True:
            try:
                item = self.email_queue.get(timeout=self.IDLE_TIMEOUT)
            except queue.Empty:
                if server is not None:
                    self._quit(server)
                    server = None
                continue
            try:
                if item is None:
                    break
                server = self._send_with_retry(server, *item)
                time.sleep(self.SEND_INTERVAL)
            finally:
                self.email_queue.task_done()
        if server is not None:
            self._quit(server)

    def _send_with_retry(self, server, recipient, subject, html):
        """Send one mail, reconnecting once if the connection was dropped. Returns the connection."""
        for attempt in (1, 2):
            try:
                if server is None:
                    server = self._connect()
                server.sendmail(self.sender, recipient, self._build_message(recipient, subject, html))
                logger.info(f"Sent email to {recipient}")
                return server
            except (smtplib.SMTPServerDisconnected, ConnectionError, OSError) as e:
                logger.warning(f"SMTP connection problem ({e}), attempt {attempt}")
                server = None
            except smtplib.SMTPException as e:
                logger.error(f"Could not send email to {recipient}: {e}")
                return server
        logger.error(f"Giving up sending email to {recipient}")
        return None

    def _build_message(self, recipient, subject, html):
        msg = MIMEMultipart()
        msg["From"] = self.sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.sender.split("@")[-1])
        msg.attach(MIMEText(f"<html><body>{html}</body></html>", "html", "utf-8"))
        return msg.as_string()

    @staticmethod
    def _quit(server):
        try:
            server.quit()
        except Exception:
            pass


if __name__ == "__main__":
    # Manual test: python -m database.SMTPMailer recipient@example.com
    import sys
    from . import config
    mailer = SMTPMailer(config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USER, config.SMTP_PASSWORD)
    mailer.send_email(sys.argv[1], "MCConnect SMTP Test", "<p>Hallo, dies ist ein Test.</p>")
    mailer.stop()
