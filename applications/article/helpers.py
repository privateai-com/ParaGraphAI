import json
import logging
import random
import re
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET  # Для парсинга XML

import cloudscraper

# import os
import requests
from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import get_channel_layer

# from scidownl import scihub_download
# from scidownl.core.updater import SearchScihubDomainUpdater
# from django.conf import settings
from django.utils import timezone

# from datetime import datetime, timedelta
# from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
from patchright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .models import Article, ArticleAuthor, ArticleContent, Author, ReferenceLink

logger = logging.getLogger(__name__)

# Пространства имен для arXiv Atom XML
ARXIV_NS = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}


def send_user_notification(
        user_id,
        task_id,
        identifier_value,
        status,
        message,
        progress_percent=None,
        article_id=None,
        created=None,
        source_api=None,
        originating_reference_link_id=None,
        analysis_data=None):

    if not user_id:
        return

    channel_layer = get_channel_layer()
    group_name = f"user_{user_id}_notifications"

    payload = {
        'task_id': task_id,
        'identifier': str(identifier_value), # Убедимся, что это строка
        'status': status,
        'message': message,
        'source_api': source_api or 'N/A'
    }

    # Добавляем опциональные поля в payload, только если они переданы
    if progress_percent is not None:
        payload['progress_percent'] = progress_percent

    if article_id is not None:
        payload['article_id'] = article_id

    if created is not None:
        payload['created'] = created

    if originating_reference_link_id is not None:
        payload['originating_reference_link_id'] = originating_reference_link_id

    if analysis_data is not None:
        payload['analysis_data'] = analysis_data

    # channel_layer.group_send(group_name, {"type": "send.notification", "payload": payload})
    async_to_sync(channel_layer.group_send)(group_name, {"type": "send.notification", "payload": payload})


def parse_crossref_authors(authors_data):
    parsed_authors = []
    if not authors_data:
        return parsed_authors
    for author_info in authors_data:
        first_name = ''
        last_name = ''
        # author = {}
        affiliations = []
        if author_info.get('given'):
            first_name = author_info['given'].strip()
        if author_info.get('family'):
            last_name = author_info['family'].strip()
        full_name = first_name if first_name else ''
        full_name += f' {last_name}' if last_name else ''
        if full_name:
            parsed_authors.append(
                {'full_name': full_name, 'first_name': first_name, 'last_name': last_name, 'affiliations': affiliations}
            )
    return parsed_authors


def parse_pubmed_authors_from_xml_metadata(author_list_node):
    parsed_authors = []
    if author_list_node is None:
        return parsed_authors
    for author_node in author_list_node.findall('./Author'):
        first_name = ''
        last_name = ''
        author = {}
        affiliations = []
        last_name_el = author_node.find('./LastName')
        fore_name_el = author_node.find('./ForeName') # or author_node.find('./Forename')
        if fore_name_el is not None and fore_name_el.text:
            first_name = fore_name_el.text.strip()
            author['first_name'] = first_name
        if last_name_el is not None and last_name_el.text:
            last_name = last_name_el.text.strip()
            author['last_name'] = last_name
        full_name = first_name if first_name else ''
        full_name += f' {last_name}' if last_name else ''
        for affiliation in author_node.findall('./AffiliationInfo'):
            affiliation_el = affiliation.find('./Affiliation')
            if affiliation_el is not None and affiliation_el.text:
                affiliations.append(affiliation_el.text.strip())
        if full_name:
            # author['full_name'] = full_name
            # author['affiliations'] = affiliations
            parsed_authors.append(
                {'full_name': full_name, 'first_name': first_name, 'last_name': last_name, 'affiliations': affiliations}
            )
            # authors = Author.objects.filter(full_name=full_name)
            # if authors:
            #     author = authors.first()
            # else:
            #     author = Author.objects.create(full_name=full_name, first_name=first_name, last_name=last_name)
            # # parsed_authors.append(author)
            # parsed_authors.append({'author_obj': author})
    print(f'*** DEBUG (parse_pubmed_authors_from_xml_metadata) parsed_authors: {parsed_authors}')
    return parsed_authors


def parse_europepmc_authors(authors_data_list):
    parsed_authors = []
    if not authors_data_list or not isinstance(authors_data_list, list):
        return parsed_authors
    for author_info_wrapper in authors_data_list:
        if isinstance(author_info_wrapper, dict) and 'author' in author_info_wrapper:
            for author_entry in author_info_wrapper['author']:
                if isinstance(author_entry, dict) and 'fullName' in author_entry:
                    full_name = author_entry['fullName'].strip()
                    if full_name:
                        # author, _ = Author.objects.get_or_create(full_name=full_name)
                        # parsed_authors.append(author)
                        parsed_authors.append(
                            {'full_name': full_name}
                        )
    return parsed_authors


# def parse_s2_authors(authors_data_list):
#     parsed_authors = []
#     if not authors_data_list or not isinstance(authors_data_list, list):
#         return parsed_authors
#     for author_info in authors_data_list:
#         if isinstance(author_info, dict) and author_info.get('name'):
#             full_name = author_info['name'].strip()
#             if full_name:
#                 author, _ = Author.objects.get_or_create(full_name=full_name)
#                 parsed_authors.append(author)
#     return parsed_authors


# def parse_arxiv_authors(entry_element):
#     parsed_authors = []
#     for author_el in entry_element.findall('atom:author', ARXIV_NS):
#         name_el = author_el.find('atom:name', ARXIV_NS)
#         if name_el is not None and name_el.text:
#             full_name = name_el.text.strip()
#             if full_name:
#                 author, _ = Author.objects.get_or_create(full_name=full_name)
#                 parsed_authors.append(author)
#     return parsed_authors


# def parse_openalex_authors(authorships_data: list) -> list:
#     """Парсинг авторов из поля 'authorships' ответа OpenAlex API."""
#     parsed_authors = []
#     if not authorships_data or not isinstance(authorships_data, list):
#         return parsed_authors

#     # authorships обычно уже отсортированы по author_position
#     for authorship in authorships_data:
#         if isinstance(authorship, dict):
#             author_info = authorship.get('author')
#             if isinstance(author_info, dict) and author_info.get('display_name'):
#                 full_name = author_info['display_name'].strip()
#                 if full_name:
#                     author, _ = Author.objects.get_or_create(full_name=full_name)
#                     parsed_authors.append(author)
#     return parsed_authors


# def reconstruct_abstract_from_inverted_index(inverted_index: dict, abstract_length: int) -> str | None:
#     """
#     Восстанавливает текст аннотации из инвертированного индекса OpenAlex.
#     :param inverted_index: Словарь инвертированного индекса.
#     :param abstract_length: Ожидаемая длина аннотации в словах (из поля abstract_length).
#     :return: Восстановленная строка аннотации или None.
#     """
#     if not inverted_index or not isinstance(inverted_index, dict) or abstract_length == 0:
#         return None

#     # Создаем список слов нужной длины
#     abstract_words = [""] * abstract_length
#     found_words = 0
#     for word, positions in inverted_index.items():
#         for pos in positions:
#             if 0 <= pos < abstract_length:
#                 abstract_words[pos] = word
#                 found_words +=1

#     # Если мы не смогли восстановить значительную часть слов, возможно, что-то не так
#     # (например, abstract_length было неверным или индекс неполный)
#     # В этом случае лучше вернуть None, чем неполный текст.
#     # Простая эвристика: если заполнено менее 70% слов, считаем неудачей.
#     if found_words < abstract_length * 0.7 and abstract_length > 10 : # Пропускаем проверку для очень коротких "абстрактов"
#         # print(f"Warning: Reconstructed abstract seems incomplete. Expected {abstract_length} words, got {found_words} words from index.")
#         # Можно вернуть ' '.join(filter(None, abstract_words)).strip() если хотим частичный результат
#         return None

#     return ' '.join(filter(None, abstract_words)).strip() # filter(None,...) убирает пустые строки, если не все позиции были заполнены


def parse_rxiv_authors(authors_data_list):
    parsed_authors = []
    if not authors_data_list or not isinstance(authors_data_list, list):
        return parsed_authors
    for author_info in authors_data_list:
        full_name = author_info.get('author_name','').strip()
        if full_name:
            if ',' in full_name:
                parts = [p.strip() for p in full_name.split(',', 1)]
                if len(parts) == 2:
                    full_name = f"{parts[1]} {parts[0]}"
            author, _ = Author.objects.get_or_create(full_name=full_name)
            parsed_authors.append(author)
    return parsed_authors


def extract_structured_text_from_jats(xml_string: str) -> dict:
    """
    Извлекает текст из JATS XML, структурируя его по основным научным секциям.
    Возвращает словарь: {'title': "...", 'abstract': "...", 'introduction': "...", ...}
    """
    if not xml_string:
        return {}

    sections = {
        "title": None,
        "abstract": None,
        "introduction": None,
        "methods": None,
        "results": None,
        "discussion": None,
        "conclusion": None,
        "other_sections": [], # Для нераспознанных, но помеченных как <sec>
        "full_body_fallback": None # Если секции не найдены, но есть <body>
    }

    try:
        root = ET.fromstring(xml_string)

        # Заголовок статьи
        article_title_el = root.find('.//front//article-meta//title-group//article-title')
        if article_title_el is not None:
            sections['title'] = "".join(article_title_el.itertext()).strip().replace('\n', ' ')

        # Абстракт
        abstract_node = root.find('.//front//article-meta//abstract')
        if abstract_node is not None:
            abstract_text_parts = []
            # Пропускаем заголовки типа "Abstract" внутри самого абстракта
            for child in abstract_node:
                if child.tag.lower() not in ['label', 'title'] or len("".join(child.itertext()).strip().split()) >= 5 :
                     abstract_text_parts.append("".join(child.itertext()).strip())
            sections['abstract'] = "\n\n".join(filter(None, abstract_text_parts))
            if not sections['abstract']: # Если нет дочерних тегов, берем весь текст абстракта
                sections['abstract'] = "".join(abstract_node.itertext()).strip().replace('\n', ' ')

        # Основной текст из <body>
        body_node = root.find('.//body')
        if body_node is not None:
            body_texts = [] # Собираем весь текст body для fallback

            for sec_node in body_node.findall('./sec'): # Ищем секции первого уровня
                sec_title_el = sec_node.find('./title')
                sec_title_text_raw = "".join(sec_title_el.itertext()).strip().lower() if sec_title_el is not None else ""

                current_sec_content_parts = []
                for p_node in sec_node.findall('.//p'): # Все параграфы внутри секции, включая вложенные <sec><p>
                    paragraph_text = "".join(p_node.itertext()).strip()
                    if paragraph_text:
                        current_sec_content_parts.append(paragraph_text)

                section_content = "\n\n".join(current_sec_content_parts)
                if not section_content:
                    continue # Пропускаем секции без текстового контента в <p>

                # Сопоставление с ключами IMRAD (можно улучшить регулярными выражениями или более сложной логикой)
                if 'introduction' in sec_title_text_raw or sec_node.get('sec-type') == 'intro':
                    sections['introduction'] = (sections['introduction'] + "\n\n" if sections['introduction'] else "") + section_content
                elif 'method' in sec_title_text_raw or 'material' in sec_title_text_raw or sec_node.get('sec-type') == 'methods':
                    sections['methods'] = (sections['methods'] + "\n\n" if sections['methods'] else "") + section_content
                elif 'result' in sec_title_text_raw or sec_node.get('sec-type') == 'results':
                    sections['results'] = (sections['results'] + "\n\n" if sections['results'] else "") + section_content
                elif 'discuss' in sec_title_text_raw or sec_node.get('sec-type') == 'discussion':
                    sections['discussion'] = (sections['discussion'] + "\n\n" if sections['discussion'] else "") + section_content
                elif 'conclu' in sec_title_text_raw or sec_node.get('sec-type') == 'conclusion': # concl, conclusion, conclusions
                    sections['conclusion'] = (sections['conclusion'] + "\n\n" if sections['conclusion'] else "") + section_content
                else:
                    sections['other_sections'].append({
                        'title': "".join(sec_title_el.itertext()).strip() if sec_title_el is not None else "Unnamed Section",
                        'text': section_content
                    })
                body_texts.append(section_content) # Добавляем в общий текст body

            if not sections['introduction'] and not sections['methods'] and not sections['results'] and body_texts: # Если не удалось распознать секции IMRAD
                sections['full_body_fallback'] = "\n\n".join(body_texts)

        # Очистка None значений
        return {k: v for k, v in sections.items() if v}

    except ET.ParseError as e:
        print(f"JATS XML Parse Error (structured): {e}")
    except Exception as e_gen:
        print(f"Generic error in structured JATS parsing: {e_gen}")
    return {} # Возвращаем пустой словарь в случае ошибки


def parse_references_from_jats(xml_string: str) -> list:
    """
    Извлекает и парсит список литературы из строки JATS XML.
    Возвращает список словарей, где каждый словарь - одна ссылка с ее метаданными
    и, что важно, с ее внутренним JATS ID (атрибут 'id' тега <ref>).
    """
    references = []
    if not xml_string:
        return references

    try:
        # Убираем default namespace для упрощения поиска через findall
        xml_string = re.sub(r'\sxmlns="[^"]+"', '', xml_string, count=1)
        root = ET.fromstring(xml_string)
        ref_list_node = root.find('.//ref-list')

        if ref_list_node is None:
            return references

        for ref_node in ref_list_node.findall('./ref'):
            # Извлекаем внутренний ID ссылки - ключ к сопоставлению
            jats_ref_id = ref_node.get('id')
            if not jats_ref_id:
                continue # Пропускаем ссылки без ID, их невозможно будет сопоставить с текстом

            ref_data = {
                'jats_ref_id': jats_ref_id,
                'doi': None,
                'title': None,
                'year': None,
                'raw_text': None,
                'authors_str': None,
                'journal_title': None,
            }

            citation_node = ref_node.find('./element-citation')

            if citation_node is None:
                citation_node = ref_node.find('./mixed-citation')

            if citation_node is None:
                citation_node = ref_node.find('./citation')

            if citation_node is not None:
                # Собираем полный текст цитаты из <mixed-citation> или формируем из <element-citation>
                ref_data['raw_text'] = " ".join(citation_node.itertext()).strip().replace('\n', ' ').replace('  ', ' ')

                # Извлекаем DOI
                doi_el = citation_node.find(".pub-id[@pub-id-type='doi']")
                if doi_el is not None and doi_el.text:
                    ref_data['doi'] = doi_el.text.strip().lower()

                # Извлекаем другие метаданные...
                title_el = citation_node.find('./article-title')
                if title_el is None:
                    title_el = citation_node.find('./chapter-title')
                if title_el is not None and title_el.text:
                    ref_data['title'] = "".join(title_el.itertext()).strip()

                year_el = citation_node.find('./year')
                if year_el is not None and year_el.text:
                    ref_data['year'] = year_el.text.strip()

                source_el = citation_node.find('./source')
                if source_el is not None:
                    ref_data['journal_title'] = source_el.text.strip()

                author_els = citation_node.findall('./string-name')
                if author_els is None:
                    author_els = citation_node.find('./person-group').findall('./name')
                if author_els is not None:
                    author_list = []
                    for author in author_els:
                        surname = None
                        given_names = None
                        full_name = ''
                        surname = author.find('./surname')
                        if surname is not None:
                            full_name += f'{surname.text.strip()}'
                        given_names = author.find('./given-names')
                        if given_names is not None:
                            full_name += f' {given_names.text.strip()}'
                        if full_name:
                            author_list.append(full_name)
                    if author_list:
                        ref_data['authors_str'] = ", ".join(author_list)

            references.append(ref_data)

    except Exception as e:
        print(f"Error during JATS reference parsing: {e}")
    return references


def download_pdf_from_pmc(pdf_url: str):
    file_content = ''
    download_url = None
    # content_type = None
    # response = None
    logger.info(f"*** (download_pdf_from_pmc) Start Download PDF from URL: {pdf_url}")

    # try:
    #     headers = {
    #         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    #         "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    #         "Accept-Language": "en-US,en;q=0.5",
    #         "DNT": "1",
    #         "Upgrade-Insecure-Requests": "1"
    #     }
    #     time.sleep(2)
    #     response = requests.get(pdf_url, timeout=60, headers=headers, stream=True, allow_redirects=True) # разрешены редиректы
    #     print(f'****** response: {response}')
    #     if response.status_code == 200:
    #         content_type = response.headers.get('content-type', '').lower()
    #         print(f'****** content_type: {content_type}')
    #         if 'application/pdf' in content_type:
    #             file_content = response.content
    #             logger.info(f"*** (REQUESTS) Download PDF from URL: {pdf_url}")
    #             # print(f'****** (REQUESTS) file_content: {file_content}')
    # except Exception as e:
    #     logger.error(f"*** (REQUESTS) Error download PDF from URL: {pdf_url} \nErro msg: {e}")

    # if not file_content:
    #     print('******** if not file_content:')

    try:
        time.sleep(5)
        # with sync_playwright() as p:
        #     browser = p.chromium.launch(headless=True)
        #     context = browser.new_context(
        #         # user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:100.0) Gecko/20100101 Firefox/100.0', # Firefox User-Agent
        #         user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36', # Chrome User-Agent
        #         java_script_enabled=True,
        #         viewport={'width': 1920, 'height': 1080},
        #         locale='en-US',
        #         # color_scheme='light', # Можно попробовать 'dark' или 'light'
        #         # timezone_id='America/New_York', # Для большей маскировки
        #         permissions=['geolocation'], # Явно запрещаем геолокацию, если не нужна
        #         # bypass_csp=True, # Использовать с ОСТОРОЖНОСТЬЮ, может нарушить работу сайта или быть обнаруженным
        #         accept_downloads=True,
        #     )
        #     # Дополнительные заголовки, которые могут помочь
        #     context.set_extra_http_headers({
        #         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        #         "Accept-Language": "en-US,en;q=0.9",
        #         "Sec-Fetch-Dest": "document",
        #         "Sec-Fetch-Mode": "navigate",
        #         "Sec-Fetch-Site": "none", # или "cross-site", если переход с Unpaywall
        #         "Sec-Fetch-User": "?1",
        #         "Upgrade-Insecure-Requests": "1",
        #         "DNT": "1", # Do Not Track
        #     })
        #     page = context.new_page()

        # def log_response_headers(response):
        #     # print(f"*** (download_pdf_from_pmc) Response headers for {response.url}:")
        #     for name, value in response.headers.items():
        #         if name == 'content-type':
        #             if value == 'application/pdf':
        #                 print(f"^^^^ (download_pdf_from_pmc) Response headers for {response.url}, {name}: {value}")

        # def handle_route(route):
        #     resp = route.fetch()
        #     headers = dict(resp.headers)
        #     headers["Content-Disposition"] = "attachment"
        #     route.fulfill(response=resp, headers=headers)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel='chrome',
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                ]
            )
            context: BrowserContext = browser.new_context()
            # context.route("**/*.pdf", lambda route, request: route.fulfill(
            #     response=context.request.get(request.url),
            #     headers={**(route.fetch()).headers, "Content-Disposition": "attachment"}
            #     )
            # )
            page: Page = context.new_page()

            try:

                def get_download_url(response):
                    time.sleep(5)
                    nonlocal download_url
                    if hasattr(response, 'url') and response.url and len(response.url) > 4 and response.url[-4:] == '.pdf':
                        download_url = response.url

                page.on("response", get_download_url)
                page.goto(pdf_url, wait_until='domcontentloaded') # 'load'

                print(f'*** DEBUG (download_pdf_from_pmc) download_url : {download_url}')
                resp = None
                if download_url:
                    # resp = page.request.get(pdf_url)
                    resp = page.request.get(download_url)

                if resp and resp.status == 200:
                    if 'content-type' in resp.headers and 'application/pdf' in resp.headers['content-type']:
                        file_content = resp.body()
                        print(f'***** (download_pdf) pdf_url: {pdf_url}, \ndownload_url: {download_url}, \nheaders: {resp.headers}, \ncontent-type: {resp.headers['content-type']}, \nfile_content: {file_content[:1000]}')
                else:
                    print(f"*** DEBUG (download_pdf_from_pmc) *resp.status not 200* Download PDF from URL: {pdf_url}. Response: {resp}")

                # # Ждём загрузку после перехода
                # with page.expect_download(timeout=60000) as download_info:
                #     # page.goto(pubmed_pdf_url, wait_until='commit') # 'domcontentloaded'
                #     # page.route("**/*.pdf", lambda route, request: handle_route(route))
                #     page.on("response", get_download_url_from_response)
                #     page.goto(pdf_url, wait_until='domcontentloaded') # 'load'
                #     # page.wait_for_timeout(5000)

                # download = download_info.value
                # if download:
                #     print(f"*** (download_pdf_from_pmc) Download PDF from URL: {pdf_url}, \ndownload: {download}")

                # # file_content = download.read()
                # with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                #     download.save_as(tmp_file.name)
                #     tmp_file.seek(0)
                #     file_content = tmp_file.read()

                # page.on("response", log_response_headers)
                # resp = page.request.get(pdf_url)

                # if resp.status == 200:
                #     if 'content-type' in resp.headers and resp.headers['content-type'] == 'application/pdf':
                #         file_content = resp.body()
                #         print(f'***** (download_pdf) pdf_url: {pdf_url}, \nheaders: {resp.headers}, \ncontent-type: {resp.headers['content-type']}, \nfile_content: {file_content[:1000]}')
                # else:
                #     logger.info(f"*** (download_pdf resp.status not 200) Download PDF from URL: {pdf_url}. Response: {resp}, response body length: {len(resp.body()) if resp.body() else ''}")
                #     print(f"*** (download_pdf resp.status not 200) Download PDF from URL: {pdf_url}. Response: {resp}, response body length: {len(resp.body()) if resp.body() else ''}, resp.body(): {resp.body()[1000] if resp.body() else ''}")

                logger.info(f"*** DEBUG (download_pdf_from_pmc) Download PDF, length: {len(file_content)} from URL: {pdf_url}")
            except Exception as e:
                logger.error(f"*** DEBUG (download_pdf_from_pmc) Error Download PDF from URL: {pdf_url}. \nError Msg: {str(e)}")
            finally:
                browser.close()

    except Exception as e:
        logger.error(f"*** DEBUG (download_pdf_from_pmc) ERROR download PDF from URL: {pdf_url} \nError msg: {e}")

    return file_content, download_url


# def download_pdf_from_rxiv(doi: str, rxiv_version: str):
#     file_content = b''
#     pdf_link = ''
#     try:

#         for api_server in ['biorxiv', 'medrxiv']:
#             url_html = f"https://www.{api_server}.org/content/{doi}v{rxiv_version}"
#             print(f"[+] (download_pdf_from_rxiv) Открываю страницу с HTML: {url_html}")

#             with sync_playwright() as pw:
#                 browser = pw.chromium.launch(headless=True)
#                 context = browser.new_context()
#                 page = context.new_page()

#                 try:
#                     page.goto(url_html, wait_until='load')
#                     time.sleep(3)

#                     # найти ссылку на PDF в DOM и перейти по ней
#                     pdf_href_attr = f'a[href$="{doi}v{rxiv_version}.full.pdf"]'
#                     pdf_link = page.get_attribute(pdf_href_attr, 'href')

#                     if pdf_link:
#                         # Полный URL (он может быть относительным)
#                         if pdf_link.startswith('/'):
#                             pdf_link = f"https://www.biorxiv.org{pdf_link}"
#                         print(f"[+] (download_pdf_from_rxiv) PDF URL Link: {pdf_link}")

#                         # скачать PDF через API playwright
#                         response = context.request.get(pdf_link)
#                         if response.ok and 'application/pdf' in response.headers.get('content-type', ''):
#                             if response.body()[:4] == b'%PDF':
#                                 if response.body() > file_content:
#                                     file_content = response.body()
#                         else:
#                             print(f"[!] (download_pdf_from_rxiv) Не удалось получить PDF (status={response.status}, url_html: {url_html})")
#                     else:
#                         print(f"*** (download_pdf_from_rxiv) pdf_link Do Not Found! Download PDF from URL: {url_html}")

#                 except Exception as e:
#                     print(f"*** (download_pdf_from_rxiv) Error Download PDF from URL: {url_html}. \nError Msg: {str(e)} \nTraceback: {traceback.print_exc()}")
#                 finally:
#                     browser.close()

#     except Exception as e:
#         print(f"*** (download_pdf_from_rxiv) Error Download PDF for DOI: {doi}, rxiv_version: {rxiv_version}. \nError Msg: {str(e)} \nTraceback: {traceback.print_exc()}")

#     print(f"[+] (download_pdf_from_rxiv) PDF Download Succes! URL Link: {pdf_link}, \nfile_content: {file_content[:100] if len(file_content) > 100 else ''}")

#     return file_content, pdf_link

def download_pdf_from_rxiv(doi: str, rxiv_version: str):
    file_content = b''
    pdf_link = ''
    try:
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})

        for api_server in ['biorxiv', 'medrxiv']:
            pdf_link = f"https://www.{api_server}.org/content/{doi}v{rxiv_version}.full.pdf"

            response = scraper.get(pdf_link)
            response.raise_for_status()
            if response.ok and 'application/pdf' in response.headers.get('content-type', ''):
                file_content = response.content
                if file_content:
                    print(f"[+] (download_pdf_from_rxiv) PDF Download Succes! URL Link: {pdf_link}")
                    break

    except Exception as e:
        print(f"*** (download_pdf_from_rxiv) Error Download PDF for DOI: {doi}, rxiv_version: {rxiv_version}, url: {pdf_link},  \nError Msg: {e} \nTraceback: {traceback.print_exc()}")

    return pdf_link, file_content


def get_xml_from_biorxiv(url):
    try:
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
        response = scraper.get(url)
        response.raise_for_status()
        return {'status': 'success', 'data': response.text}
    except Exception as e:
        print(f"*** (get_xml_from_biorxiv) Error Download XML from URL: {url} \nError Msg: \n{e} \nTraceback: \n{traceback.print_exc()}")
        return {'status': 'error', 'message': str(e)}


def download_pdf_from_scihub_box(doi: str):
    """Downloads a PDF from Sci-Hub using Playwright.

    Args:
        doi: The DOI of the paper to download.
    """
    file_content = ''
    pdf_url = ''
    domain = "https://sci-hub.box"
    try:
        time.sleep(2)
        with sync_playwright() as playwright:
            with playwright.chromium.launch(
                channel='chrome',
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                ]
            ) as browser:
                context: BrowserContext = browser.new_context()
                page: Page = context.new_page()

                try:
                    # page = context.new_page()
                    # Navigate to Sci-Hub and search for the paper
                    page.goto(domain)
                    time.sleep(3)
                    page.fill("textarea[name='request']", doi)
                    page.click("button[type='submit']")
                    time.sleep(3) # Wait for the page to load
                    logger.info(f'(download_pdf_from_scihub_box) DOI: {doi}, page_content: \n{page.content()}')

                    pdf_el = page.locator('#pdf')
                    raw_src = pdf_el.get_attribute("src")

                    # Create URL
                    if raw_src:
                        clear_src = []
                        for s in raw_src.replace('//', '/').split('/'):
                            if s != '':
                                clear_src.append(s)

                        if clear_src[0].startswith('http'):
                            clear_src.pop(0)

                        if 'sci-hub.' in clear_src[0]:
                            clear_src.insert(0, 'https:/')
                        else:
                            clear_src.insert(0, domain)

                        pdf_url = '/'.join(clear_src)

                    if pdf_url:
                        # with page.expect_download(timeout=30000) as download_info:
                        #     page.evaluate("url => window.open(url)", pdf_url)
                        # download = download_info.value

                        # with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                        #     download.save_as(tmp_file.name)
                        #     tmp_file.seek(0)
                        #     file_content = tmp_file.read()

                        resp = page.request.get(pdf_url)
                        if resp and resp.status == 200:
                            if 'content-type' in resp.headers and resp.headers['content-type'] == 'application/pdf':
                                file_content = resp.body()
                            # print(f'***** (download_pdf_from_scihub_box) DOI: {doi}, \nheaders: {resp.headers}, \ncontent-type: {resp.headers['content-type']}, \nfile_content[:4]: {file_content[:4]}, file_content: {file_content[:100]}')
                        else:
                            logger.info(f"(download_pdf_from_scihub_box) Download PDF from URL: {pdf_url}. Response: {resp}")

                        logger.info(f"(download_pdf_from_scihub_box) DOI: {doi}, Download PDF from URL: {pdf_url}")
                    else:
                        logger.info(f"(download_pdf_from_scihub_box) DOI: {doi}, PDF URL Not Found.")

                except Exception as e:
                    logger.error(f"(download_pdf_from_scihub_box) Error Download PDF from URL: {pdf_url}. \nError Msg: {str(e)}")
                finally:
                    browser.close()

    except Exception as e:
        logger.error(f'(download_pdf_from_scihub_box)  DOI: {doi}, pdf_url: {pdf_url}. \ntraceback: {traceback.format_exc()}. \nAn error occurred: {e}')

    return file_content, pdf_url


def send_prompt_to_grok(prompt: str) -> str | None:
    response_text = ''
    with sync_playwright() as playwright:
        with playwright.chromium.launch(
            channel='chrome',
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
            ]
        ) as browser:
            context: BrowserContext = browser.new_context()
            page: Page = context.new_page()
            #prompt = "Hello! What is the capital of France?"

            try:
                # Navigate to Grok chat page and wait for it to load
                print("*** Navigating to https://grok.com/chat...")
                page.goto("https://grok.com/chat", wait_until="domcontentloaded")
                page.wait_for_timeout(1000 * random.uniform(2.5, 5.5))
                time.sleep(10)

                # Wait for the page to be fully loaded
                # page.wait_for_load_state("networkidle", timeout=30000)
                # print(f'*** DEBUG (send_prompt_to_grok) page_content: \n{page.content()}')

                textarea_selector = 'form textarea'
                # page.wait_for_selector(textarea_selector)
                # print("*** DEBUG (send_prompt_to_grok) textarea_selector loaded.")
                # Input the prompt
                page.fill(selector=textarea_selector, value=prompt)
                print("*** DEBUG (send_prompt_to_grok) Found textarea.")
                page.wait_for_timeout(1000 * random.uniform(2.5, 5.5))

                # Click submit button
                submit_button_selector = 'form button[type="submit"]'
                print("*** DEBUG (send_prompt_to_grok) Looking for submit button...")
                page.click(submit_button_selector)
                print("*** (send_prompt_to_grok) Found submit button.")
                page.wait_for_timeout(1000 * random.uniform(2.5, 5.5))

                print("*** DEBUG (send_prompt_to_grok) Submitted prompt.")
                time.sleep(10)

                response_selector = '#last-reply-container .response-content-markdown code'
                print("*** DEBUG (send_prompt_to_grok) Waiting for response...")
                try:
                    page.wait_for_selector(response_selector, timeout=30000)
                    response_text = page.text_content(response_selector)
                    print('*' * 60)
                    print(f'*** DEGUG (send_prompt_to_grok)response text: {response_text}')
                    print('*' * 60)
                except Exception as e:
                    logger.error(f"*** DEBUG (send_prompt_to_grok) Error: {str(e)} \ntraceback: {traceback.format_exc()}")

            except Exception as e:
                logger.error(f"*** DEBUG (send_prompt_to_grok) ERROR: {str(e)} \ntraceback: {traceback.format_exc()}")
            finally:
                browser.close()

    return response_text


def find_orcid(last_name: str, doi: str | None = None, pmid: str | None = None) -> dict:
    """
    Ищет ORCID автора по фамилии, DOI или PUBMED ID статьи через ORCID API.

    Args:
        last_name (str): Фамилия автора.
        doi (str, optional): DOI статьи для дополнительной фильтрации.
        pmid (str, optional): PMID статьи для дополнительной фильтрации.

    Returns:
        ORCID ID и его данные.
    """
    orcid_id = ''
    orcid_data = {}
    try:
        # Базовый URL публичного API ORCID
        base_url = "https://pub.orcid.org/v3.0"
        headers = {'Accept': 'application/json'}
        results = []

        if not last_name:
            error_msg = "No last name for ORCID search."
            print(error_msg)
            return {'status': 'error', 'message': error_msg}

        if not doi and not pmid:
            error_msg = "Required identifiers for ORCID search are missing."
            print(error_msg)
            return {'status': 'error', 'message': error_msg}

        # Выполняем поиск по фамилии и doi
        if doi:
            search_url = f"{base_url}/search/?q=family-name:{last_name.strip()}+AND+doi-self:{doi.strip()}"
            print(f'1 search_url: {search_url}')

            time.sleep(2)
            response = requests.get(search_url, headers=headers)
            response.raise_for_status()
            data = response.json()

            if data:
                num_found = data.get('num-found', 0)
                print(f'1 Results found: {num_found}')
                results = data.get('result', [])
                print(f'1 results: {results}')

        # Выполняем поиск по фамилии и pmid
        if not results or num_found !=1 and pmid:
            if doi and pmid:
                search_url = f"{base_url}/search/?q=family-name:{last_name.strip()}+AND+pmid-self:{pmid.strip()}"
                print(f'2 search_url: {search_url}')

                time.sleep(2)
                response = requests.get(search_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                if data:
                    num_found = data.get('num-found', 0)
                    print(f'2 Results found: {num_found}')
                    results = data.get('result', [])
                    print(f'2 results: {results}')

        # Получаем все данные для orcid_id
        if results and num_found == 1:
            orcid_id = results[0].get('orcid-identifier', {}).get('path')
            print(f'orcid_id: {orcid_id}')
            if orcid_id:
                orcid_url = f"{base_url}/{orcid_id}"
                print(f'orcid_url: {orcid_url}')
                try:
                    time.sleep(2)
                    orcid_response = requests.get(orcid_url, headers=headers)
                    orcid_response.raise_for_status()
                    orcid_data = orcid_response.json()
                except requests.exceptions.RequestException as e:
                    error_msg = f"Error when retrieving data for ORCID {orcid_id}: {e}"
                    print(error_msg)
                    return {'status': 'error', 'message': error_msg}
        else:
            error_msg = f"No data for ORCID {orcid_id}"
            print(error_msg)
            return {'status': 'error', 'message': error_msg}

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 500:
            error_msg = f"The ORCID server returned an error 500 for the query: {search_url}. Try again later."
        else:
            error_msg = f"Error when making a request to ORCID API: {e}"
        print(error_msg)
        return {'status': 'error', 'message': error_msg}
    except requests.exceptions.RequestException as e:
        error_msg = f"Error when making a request to ORCID API: {e}"
        print(error_msg)
        return {'status': 'error', 'message': error_msg}

    return {'status': 'success', 'orcid_id': orcid_id, 'orcid_data': orcid_data}
