import re
from pathlib import Path
from dataclasses import dataclass
from playwright.sync_api import (
    Page, 
    BrowserContext, 
    sync_playwright, 
    Error as pwerror,
    TimeoutError as pwtimeout,
    expect as pwexpect
    )

try:
    import credentials
except ImportError:
    raise RuntimeError('credentials file cant open, create it first')

@dataclass
class DataPx:
    accession: str
    name: str
    exam: str
    wa_number: str =''
    date_exam: str = ''

    def __post_init__(self) -> None:
        self.name = self.name.strip().upper()
        self.accession = str(self.accession).strip()
        self.wa_number = str(self.wa_number).strip()

        raw_wa = re.sub(r'\D', '', str(self.wa_number))
        if raw_wa.startswith('0'):
            self.wa_number = '62'+raw_wa[1:]
        else:
            self.wa_number = raw_wa



class PacsExtractor:
    def __init__(self, context: BrowserContext, username: str, password: str, folder_download: str | Path, url_login: str) -> None:
        self.context = context
        self.username = username
        self.password = password
        self.folder_download = Path(folder_download)
        self.folder_download.mkdir(parents=True, exist_ok=True)
        self.url_login = url_login


    @staticmethod
    def _clean_text(text:str) -> str:
        clean_text = re.sub(r'[\\/*?:"<>|]', "-", text)
        return " ".join(clean_text.split())


    def _login(self, page: Page) -> bool:
        print('accessing pacs')

        # check server & internet connection
        try:
            response = page.goto(self.url_login, timeout=20000)
            if response and response.status >= 400:
                print(f"server error: HTTP Status {response.status}")
                return False
        except pwtimeout: 
            print('timeout server')
            return False
        except pwerror as err:
            print('failed connect to internet or server')
            print(f"detail error: {err.message.splitlines()[0]}")
            return False

        # pop up cahe & cookies
        try:
            page.get_by_role("link", name=re.compile("Accept", re.IGNORECASE)).click(timeout=8000)
        except pwtimeout:
            pass

        # login
        try:
            print('process login...')
            page.get_by_role("textbox", name="Enter your username").fill(self.username, timeout=10000)
            page.get_by_role("textbox", name="Enter your password").fill(self.password, timeout=10000)
            page.get_by_role("button", name=re.compile("Sign In", re.IGNORECASE)).click()
        except pwerror:
            print('no login form found')
            print(f"detail error: {pwerror.message.splitlines()[0]}")
            return False

        # verify dashboard loaded
        try:
            accesion_column = page.get_by_role("textbox", name="Accession No.")
            pwexpect(accesion_column).to_be_visible(timeout=15000)
            print('login success')
            return True
        except AssertionError:
            print('login failed, check username or password or server error')
            return False


    def _search_accession(self, page: Page, accession: str) -> None:
        print(f'searching accession {accession}...')

        search_box = page.get_by_role("textbox", name="Accession No.")
        search_box.fill(accession)
        page.get_by_role("button", name=re.compile("Search", re.IGNORECASE)).click()

        page.wait_for_load_state("load")

        #verify only one result
        accession_cell = page.get_by_role("cell", name=accession, exact=True)
        try:
            pwexpect(accession_cell).to_be_visible(timeout=8000)
        except AssertionError:
            raise ValueError(f"accession {accession} not found")

        lines = page.locator("tbody tr")
        try:
            pwexpect(lines).to_have_count(1, timeout=5000)
        except AssertionError:
            line_count = lines.count()
            raise ValueError(f"found {line_count} result, expected one result only for accession. cannot continue")

        print('accession found, start extracting data...')

        page.wait_for_load_state("load")
        page.get_by_role("cell").first.click()


    def _download_pdf(self, page: Page, px: DataPx) -> Path:
        print('downloading pdf report...')

        with page.expect_popup() as page1_info:
            page.get_by_role("link", name=re.compile("Show Report", re.IGNORECASE)).click()
        page_report = page1_info.value

        extra_popup: list[Page] = []
        page_report.on("popup", lambda popup: extra_popup.append(popup))

        try:
            page_report.wait_for_load_state("load")

            btn_link_pdf = page_report.get_by_role("link", name=re.compile("Download PDF", re.IGNORECASE))
            btn_download = page_report.get_by_role("button", name=re.compile("Download", re.IGNORECASE))
            target_button = btn_link_pdf.or_(btn_download).first

            pwexpect(target_button).to_be_visible(timeout=15000)

            try:
                with page_report.expect_download(timeout=2000) as download_info:
                    target_button.click()
            except pwtimeout:
                with page_report.expect_download(timeout=15000) as download_info:
                    target_button.click()


            downloadpw = download_info.value

            base_name = Path(downloadpw.suggested_filename).stem
            clean_name = self._clean_text(px.name)
            exam_name = self._clean_text(px.exam)
            filename = f"{base_name} - {clean_name}-{exam_name}.pdf"

            download_path = Path(self.folder_download) / filename
            downloadpw.save_as(download_path)

            print(f'pdf report saved to {download_path}')
            return download_path

        finally:
            for popup in extra_popup:
                if not popup.is_closed():
                    popup.close()
            if not page_report.is_closed():
                page_report.close()


    def _create_link(self, page: Page) -> str:
        print('create link pacs...')

        page.get_by_role("button", name=re.compile("Share", re.IGNORECASE)).click()
        with page.expect_popup() as page2_info:
            page.get_by_role("link", name="Via link").click()
        page_share = page2_info.value
        page_share.wait_for_load_state("load")

        try:
            page_share.get_by_role("textbox", name="Set Expiration").click()
            page_share.get_by_title("Clear selection").click()
            page_share.get_by_title("Close the picker").click()

            page_share.locator("div:nth-child(4) > .switch > .checkmark").click()
            page_share.get_by_role ("button", name="Share", exact=True).click()

            link_position = page_share.locator("div.alert-success", has_text="Below is the share link to access the study:")
            pwexpect(link_position).to_be_visible(timeout=10000)
            page_share.wait_for_load_state("load")

            position = link_position.inner_text()
            match = re.search(r"https?://\S+", position)
            if not match:
                raise ValueError(f"Link pacs not found: {position}")

            link_pacs = match.group(0).strip()
            print(f"link pacs found {link_pacs}")
            return link_pacs

        finally:
            page_share.close()


    def extracting(self, px: DataPx) -> tuple[Path, str]:
        page = self.context.new_page()
        try:
            if not self._login(page):
                raise RuntimeError('canceled because cant login to pacs')
            
            self._search_accession(page, px.accession)
            path_pdf = self._download_pdf(page, px)
            link_pacs = self._create_link(page)

            print(f'all data px {px.accession} successfully extract')
            return path_pdf, link_pacs
        
        finally:
            page.close()



# TESTING
if __name__ == "__main__":
    folder_pdf = Path(__file__).resolve().parent/"hasil_pdf"

    test_px = DataPx(**credentials.data_px)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()

        bot = PacsExtractor(
            context = context,
            url_login = credentials.pacs_url,
            username = credentials.pacs_username,
            password = credentials.pacs_password,
            folder_download = folder_pdf
        )

        try:
            file_pdf, url_pacs = bot.extracting(test_px)
            print('uji coba stateless OOP berhasil')
            print(f"file PDF: {file_pdf}")
            print(f"link pacs: {url_pacs}")

        except Exception as e:
            print(f"proses dihentikan: {e}")

        finally:
            context.close()
            browser.close()




