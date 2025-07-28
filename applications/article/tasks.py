import json
import os
# import pprint
import re
import time
import xml.etree.ElementTree as ET  # Для парсинга XML

import requests
from openai import OpenAI

from celery import chord, group, shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction  # utils для OperationalError
# from django.db import utils as db_utils
from django.utils import timezone

from .helpers import (
    # extract_structured_text_from_bioc,
    # sanitize_for_json_serialization,
    # get_pmc_pdf,
    # download_pdf,
    download_pdf_from_pmc,
    download_pdf_from_scihub_box,
    download_pdf_from_rxiv,
    extract_structured_text_from_jats,
    # parse_arxiv_authors,
    parse_crossref_authors,
    parse_europepmc_authors,
    # parse_openalex_authors,
    # parse_pubmed_authors,
    parse_pubmed_authors_from_xml_metadata,
    # extract_text_from_jats_xml,
    # extract_text_from_bioc_json,
    parse_references_from_jats,
    parse_rxiv_authors,
    # parse_s2_authors,
    # reconstruct_abstract_from_inverted_index,
    send_prompt_to_grok,
    send_user_notification,
    find_orcid,
    get_xml_from_biorxiv,
)
from .models import AnalyzedSegment, Article, ArticleAuthor, ArticleContent, Author, ReferenceLink

# --- Константы API из настроек ---
APP_EMAIL = getattr(settings, 'APP_EMAIL', 'transposons.chat@gmail.com') # РЕАЛЬНЫЙ EMAIL в settings.py или здесь
NCBI_API_KEY = getattr(settings, 'NCBI_API_KEY', None) # Из настроек, если есть
NCBI_TOOL_NAME = 'ScientificPapersApp'
NCBI_ADMIN_EMAIL = APP_EMAIL
FIND_DOI_TASK_SOURCE_NAME = 'FindDOITask'
PIPELINE_DISPATCHER_SOURCE_NAME = 'PipelineDispatcher'

# Пространства имен для arXiv Atom XML
ARXIV_NS = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}

USER_AGENT_LIST = [
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36', # Linux, Chrome
]


# --- Диспетчерская задача ---
@shared_task(bind=True)
def process_article_pipeline_task(
    self,
    identifier_value: str,
    identifier_type: str,
    user_id: int,
    originating_reference_link_id: int = None):

    try:
        pipeline_task_id = self.request.id # ID самой диспетчерской задачи
        pipeline_display_name = f"{identifier_type.upper()}:{identifier_value}"
        if originating_reference_link_id:
            pipeline_display_name += f" (для ref_link: {originating_reference_link_id})"

        send_user_notification(
            user_id,
            pipeline_task_id,
            pipeline_display_name,
            'PIPELINE_START',
            f'Run a processing pipeline for: {pipeline_display_name}',
            progress_percent=0,
            source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
            originating_reference_link_id=originating_reference_link_id,
        )

        article_owner = None
        if user_id:
            try:
                article_owner = User.objects.get(id=user_id)
            except User.DoesNotExist:
                send_user_notification(
                    user_id,
                    pipeline_task_id,
                    pipeline_display_name,
                    'PIPELINE_FAILURE',
                    f'User ID {user_id} not found.',
                    source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                    originating_reference_link_id=originating_reference_link_id,
                )
                return {'status': 'error', 'message': f'User ID {user_id} not found.'}

        if not article_owner:
            send_user_notification(
                user_id,
                pipeline_task_id,
                pipeline_display_name,
                'PIPELINE_FAILURE',
                'The user is not specified for the conveyor.',
                source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': 'User not specified for pipeline.'}

        # --- Шаг 1: Начальное создание/получение статьи и определение основного DOI ---
        article = None
        created_article_in_pipeline = False # Флаг, была ли статья создана именно этим запуском диспетчера

        # Готовим параметры для поиска/создания статьи
        initial_article_lookup_kwargs = {}
        effective_doi_for_subtasks = None # DOI, который будет использоваться для большинства подзадач
        identifier_type_upper = identifier_type.upper()

        if identifier_type_upper == 'DOI':
            effective_doi_for_subtasks = identifier_value.lower()
            initial_article_lookup_kwargs['doi'] = effective_doi_for_subtasks
        elif identifier_type_upper == 'PMID':
            initial_article_lookup_kwargs['pubmed_id'] = identifier_value
        elif identifier_type_upper == 'ARXIV':
            initial_article_lookup_kwargs['arxiv_id'] = identifier_value.replace('arXiv:', '').split('v')[0].strip()

        if not initial_article_lookup_kwargs:
            send_user_notification(
                user_id,
                pipeline_task_id,
                pipeline_display_name,
                'PIPELINE_FAILURE',
                f'Unsupported identifier type: {identifier_type}',
                source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': f'Unsupported identifier type: {identifier_type}'}

        try:
            with transaction.atomic():
                creation_defaults = {
                    'user': article_owner,
                    'title': f"Article in processing: {pipeline_display_name}",
                    'is_user_initiated': originating_reference_link_id is None # True если это прямой вызов, False если для ссылки
                }

                article, created_article_in_pipeline = Article.objects.select_for_update().get_or_create(
                    **initial_article_lookup_kwargs,
                    defaults=creation_defaults
                )

                # Если статья уже существовала, но была не "основной", а пользователь сейчас добавляет ее напрямую
                if not created_article_in_pipeline and article.user == article_owner and \
                    not article.is_user_initiated and originating_reference_link_id is None:
                        article.is_user_initiated = True
                        article.save(update_fields=['is_user_initiated', 'updated_at'])

            if created_article_in_pipeline:
                send_user_notification(
                    user_id,
                    pipeline_task_id,
                    pipeline_display_name,
                    'PIPELINE_PROGRESS',
                    f'The pre-record for article ID {article.id} has been created (user_initiated={article.is_user_initiated}).', source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                    originating_reference_link_id=originating_reference_link_id,
                )
            else:
                send_user_notification(
                    user_id,
                    pipeline_task_id,
                    pipeline_display_name,
                    'PIPELINE_PROGRESS',
                    f'Found an existing article ID {article.id} (user_initiated={article.is_user_initiated}). Update...', source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                    originating_reference_link_id=originating_reference_link_id,
                )

            # Обновляем effective_doi_for_subtasks, если он был найден/установлен в статье
            if not effective_doi_for_subtasks and article.doi:
                effective_doi_for_subtasks = article.doi

        except Exception as e:
            send_user_notification(
                user_id,
                pipeline_task_id,
                pipeline_display_name,
                'PIPELINE_FAILURE',
                f'Error when creating/receiving an article: {str(e)}',
                source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': f'Error accessing article entry: {str(e)}'}

        article_id_for_subtasks = article.id # ID статьи для всех подзадач

        send_user_notification(
            user_id,
            pipeline_task_id,
            pipeline_display_name,
            'PIPELINE_PROGRESS',
            'Start getting data from sources...',
            progress_percent=5,
            source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
            originating_reference_link_id=originating_reference_link_id,
        )

        # --- Шаг 2: Вызов задач ---
        try:

            header_tasks = []
            # Обработка ссылок включается только если это не обработка уже существующей ссылки (т.е. "корневой" вызов)
            should_process_refs_in_crossref = (originating_reference_link_id is None)
            callback = process_data_task.s(
                article_id=article_id_for_subtasks,
                user_id=user_id,
                originating_reference_link_id=originating_reference_link_id,
            )

            if identifier_type_upper == 'DOI':
                header_tasks = [
                    fetch_data_from_crossref_task.s(
                        doi=effective_doi_for_subtasks,
                        user_id=user_id,
                        process_references=should_process_refs_in_crossref,
                        originating_reference_link_id=originating_reference_link_id,
                    ),
                    fetch_data_from_pubmed_task.s(
                        identifier_value=effective_doi_for_subtasks,
                        identifier_type='DOI',
                        user_id=user_id,
                        originating_reference_link_id=originating_reference_link_id,
                        # article_id_to_update=article_id_for_subtasks
                    ),
                    fetch_data_from_europepmc_task.s(
                        identifier_value=effective_doi_for_subtasks,
                        identifier_type='DOI',
                        user_id=user_id,
                        originating_reference_link_id=originating_reference_link_id,
                        # article_id_to_update=article_id_for_subtasks
                    ),
                    fetch_data_from_rxiv_task.s(
                        doi=effective_doi_for_subtasks,
                        user_id=user_id,
                        originating_reference_link_id=originating_reference_link_id,
                    )
                ]
            elif identifier_type_upper == 'PMID':
                header_tasks = [
                    fetch_data_from_pubmed_task.s(
                        identifier_value=identifier_value,
                        identifier_type='PMID',
                        user_id=user_id,
                        originating_reference_link_id=originating_reference_link_id,
                        # article_id_to_update=article_id_for_subtasks
                    )
                ]

            result = chord(header_tasks)(callback)

            send_user_notification(
                user_id,
                pipeline_task_id,
                effective_doi_for_subtasks,
                'SUBTASK_STARTED',
                f'Subtask started: {result.task_id}. Waiting for results...',
                # progress_percent=10,
                source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                originating_reference_link_id=originating_reference_link_id,
            )

        except Exception as e:
            print(f'*** DEBUG (process_article_pipeline_task) Error: {e}')
            send_user_notification(
                user_id,
                pipeline_task_id,
                effective_doi_for_subtasks,
                'PIPELINE_ERROR',
                f'Failed to start tasks: {e}',
                source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': f'Error run group tasks: {str(e)}'}

    except Exception as e:
        print(f'*** DEBUG (process_article_pipeline_task) ERROR: {e}')
        send_user_notification(
            user_id,
            pipeline_task_id,
            pipeline_display_name,
            'PIPELINE_FAILURE',
            f'Error: {str(e)}',
            source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
            originating_reference_link_id=originating_reference_link_id,
        )
        return {'status': 'error', 'message': f'Error process article pipeline task: {str(e)}'}

    # Финальное уведомление от диспетчера (progress_percent=100)
    send_user_notification(
        user_id,
        pipeline_task_id,
        pipeline_display_name,
        'PIPELINE_COMPLETE',
        'The processing pipeline has completed setting up all tasks.',
        progress_percent=100,
        source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
        originating_reference_link_id=originating_reference_link_id,
    )
    return {
        'status': 'success',
        'message': 'The task pipeline is up and running.',
        'pipeline_task_id': pipeline_task_id,
        'article_id': article_id_for_subtasks,
    }


@shared_task(bind=True, default_retry_delay=60)
def process_data_task(
    self,
    article_data_list: list,
    article_id: int,
    user_id: int,
    originating_reference_link_id: int = None):

    try:
        task_id = self.request.id
        query_display_name = "PROCESS_DATA_TASK"
        article_owner = None
        article = None

        if not article_data_list:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                'No data to process...',
                # source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': 'PROCESS_DATA_TASK: Data not found.'}

        if user_id:
            try:
                article_owner = User.objects.get(id=user_id)
            except User.DoesNotExist:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'User ID {user_id} not found.',
                    # source_api=PIPELINE_DISPATCHER_SOURCE_NAME,
                    originating_reference_link_id=originating_reference_link_id,
                )
                return {'status': 'error', 'message': f'PROCESS_DATA_TASK: User ID {user_id} not found.'}

        if article_owner and article_id:
            try:
                article = Article.objects.get(id=article_id, user=article_owner)
            except Article.DoesNotExist:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'Article ID {article_id} (to update) not found/not owned by user.',
                    # source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id,
                )
                return {'status': 'error', 'message': f'PROCESS_DATA_TASK: Article ID {article_id} not found.'}

        if not article:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                f'Article ID {article_id} (to update) not found.',
                # source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': f'PROCESS_DATA_TASK: Article ID {article_id} not found.'}

        title = ''
        # authors = []
        abstract = ''
        # doi = ''
        # pubmed_id = ''
        # pmc_id = ''
        # arxiv_id = ''
        # cleaned_text_for_llm = ''
        # is_manually_added_full_text = False
        # pdf_file = None
        # pdf_text = ''
        # primary_source_api = ''
        publication_date = None
        journal_name = ''
        oa_status = ''
        best_oa_url = ''
        best_oa_pdf_url = ''
        oa_license = ''
        # is_user_initiated = False
        # structured_content = {}

        current_api_name = ''
        # article_content_format_type = ''
        # article_content = ''

        author_list = []
        full_text = {'primary_source_api': '', 'full_text_xml': ''}
        full_text_xml_pmc = ''
        full_text_xml_europepmc = ''
        full_text_xml_rxvi = ''

        for data in article_data_list:
            if data['status'] == 'success':
                if 'article_data' in data and data['article_data']:
                    article_data = data['article_data']
                    if article_data.get('current_api_name'):
                        current_api_name = article_data.get('current_api_name', '')
                        article.primary_source_api = current_api_name
                    if article_data.get('title') and len(article_data.get('title', '')) > len(title):
                        title = article_data.get('title')
                    if article_data.get('abstract') and len(article_data.get('abstract', '')) > len(abstract):
                        abstract = article_data.get('abstract')
                    # if article_data.get('doi') and len(article_data.get('doi', '')) > len(doi):
                    if 'doi' in article_data and article_data['doi'] and not article.doi:
                        article.doi = article_data.get('doi')
                    # if article_data.get('pubmed_id') and len(article_data.get('pubmed_id', '')) > len(pubmed_id):
                    if 'pubmed_id' in article_data and article_data['pubmed_id'] and not article.pubmed_id:
                        article.pubmed_id = article_data.get('pubmed_id')
                    # if article_data.get('pmc_id') and len(article_data.get('pmc_id', '')) > len(pmc_id):
                    if 'pmc_id' in article_data and article_data['pmc_id'] and not article.pmc_id:
                        article.pmc_id = article_data.get('pmc_id')
                    # if article_data.get('arxiv_id') and len(article_data.get('arxiv_id', '')) > len(arxiv_id):
                    if 'arxiv_id' in article_data and article_data['arxiv_id'] and not article.arxiv_id:
                        article.arxiv_id = article_data.get('arxiv_id')
                    if article_data.get('journal_name') and len(article_data.get('journal_name', '')) > len(journal_name):
                        journal_name = article_data.get('journal_name')
                    if article_data.get('oa_status') and len(article_data.get('oa_status', '')) > len(oa_status):
                        oa_status = article_data.get('oa_status')
                    if article_data.get('best_oa_url') and len(article_data.get('best_oa_url', '')) > len(best_oa_url):
                        best_oa_url = article_data.get('best_oa_url')
                    if article_data.get('best_oa_pdf_url') and len(article_data.get('best_oa_pdf_url', '')) > len(best_oa_pdf_url):
                        best_oa_pdf_url = article_data.get('best_oa_pdf_url')
                    if article_data.get('oa_license') and len(article_data.get('oa_license', '')) > len(oa_license):
                        oa_license = article_data.get('oa_license')
                    if article_data.get('publication_date') and article_data.get('publication_date'):
                        publication_date = article_data.get('publication_date')

                    if article_data.get('authors'):
                        for new_author in article_data.get('authors'):
                            flag_is_exist = False
                            for i, author in enumerate(author_list):
                                if new_author['full_name'] == author['full_name']:
                                    flag_is_exist = True
                                    if 'affiliations' in new_author and new_author['affiliations']:
                                        for affiliation in new_author['affiliations']:
                                            if affiliation not in author['affiliations']:
                                                author_list[i]['affiliations'].append(affiliation)
                            if not flag_is_exist:
                                author_list.append(new_author)

                    if current_api_name == settings.API_SOURCE_NAMES['CROSSREF']:
                        print("*** DEBUG (process_data_task) if current_api_name == settings.API_SOURCE_NAMES['CROSSREF']:")
                        ArticleContent.objects.update_or_create(
                            article=article,
                            source_api_name=current_api_name,
                            format_type=article_data.get('article_content_format_type', ''),
                            defaults={'content': json.dumps(article_data.get('article_content', {}),)}
                        )

                    elif current_api_name == settings.API_SOURCE_NAMES['PUBMED']:
                        print("*** DEBUG (process_data_task) elif current_api_name == settings.API_SOURCE_NAMES['PUBMED']:")
                        if article_data.get('article_contents'):
                            article_contents = article_data.get('article_contents')
                            # print(f'********* article_contents: {article_contents}')
                            for key, val in article_contents.items():
                                print(f'*** DEBUG (process_data_task) key: {key}')
                                if key and val:
                                    ArticleContent.objects.update_or_create(
                                        article=article,
                                        source_api_name=current_api_name,
                                        format_type=key,
                                        defaults={'content': val}
                                    )

                                    # Если получен полный текст из PMC
                                    if key == 'full_text_xml_pmc':
                                        full_text_xml_pmc = val
                                        if len(val) > len(full_text['full_text_xml']):
                                            full_text = {
                                                'primary_source_api': current_api_name,
                                                'full_text_xml': val,
                                            }

                    elif current_api_name == settings.API_SOURCE_NAMES['EUROPEPMC']:
                        print("*** DEBUG (process_data_task) elif current_api_name == settings.API_SOURCE_NAMES['EUROPEPMC']:")
                        if article_data.get('article_contents'):
                            article_contents = article_data.get('article_contents')
                            # print(f'********* article_contents: {article_contents}')
                            for key, val in article_contents.items():
                                print(f'*** DEBUG (process_data_task) EUROPEPMC key: {key}')
                                if key and val:
                                    ArticleContent.objects.update_or_create(
                                        article=article,
                                        source_api_name=current_api_name,
                                        format_type=key,
                                        defaults={'content': val}
                                    )

                                    # Если получен полный текст из EUROPEPMC
                                    if key == 'full_text_xml_europepmc':
                                        full_text_xml_europepmc = val
                                        if len(val) > len(full_text['full_text_xml']):
                                            full_text = {
                                                'primary_source_api': current_api_name,
                                                'full_text_xml': val,
                                            }

                    elif current_api_name == settings.API_SOURCE_NAMES['RXIV']:
                        print("*** DEBUG (process_data_task) elif current_api_name == settings.API_SOURCE_NAMES['RXIV']:")
                        if article_data.get('article_contents'):
                            article_contents = article_data.get('article_contents')

                            for key, val in article_contents.items():
                                print(f'*** DEBUG (process_data_task) key: {key}')
                                if key and val:
                                    ArticleContent.objects.update_or_create(
                                        article=article,
                                        source_api_name=current_api_name,
                                        format_type=key,
                                        defaults={'content': val}
                                    )

                                    # Если получен полный текст
                                    if key == 'full_text_xml_rxvi':
                                        full_text_xml_rxvi = val
                                        if len(val) > len(full_text['full_text_xml']):
                                            full_text = {
                                                'primary_source_api': current_api_name,
                                                'full_text_xml': val,
                                            }

        article.title = title
        article.abstract = abstract
        # article.doi = doi
        # article.pubmed_id = pubmed_id
        # article.pmc_id = pmc_id
        # article.arxiv_id = arxiv_id
        article.journal_name = journal_name
        article.oa_status = oa_status
        article.best_oa_url = best_oa_url
        article.best_oa_pdf_url = best_oa_pdf_url
        article.oa_license = oa_license
        article.publication_date = publication_date

        pdf_file_name = None
        pdf_to_save = None
        pdf_url = None

        if full_text['full_text_xml']:
            current_api_name = full_text['primary_source_api']
            structured_data = extract_structured_text_from_jats(full_text['full_text_xml'])
            if structured_data:
                article.structured_content = structured_data
                article.regenerate_cleaned_text_from_structured() # Вызываем метод модели для обновления cleaned_text_for_llm
                article.primary_source_api = current_api_name

            # --- Парсинг и сохранение ссылок из полного текста ---
            process_references_flag = (originating_reference_link_id is None)
            if process_references_flag:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'PROGRESS',
                    f'Extract links from the full text of {current_api_name}...',
                    progress_percent=85,
                    source_api=current_api_name
                )

                parsed_references = parse_references_from_jats(full_text['full_text_xml'])
                if parsed_references:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'INFO',
                        f'Found {len(parsed_references)} references in full text. Processing...',
                        source_api=current_api_name
                    )

                    processed_jats_ref_count = 0
                    for ref_data in parsed_references:
                        ref_doi_jats = ref_data.get('doi')
                        ref_raw_text_jats = ref_data.get('raw_text')
                        jats_ref_id = ref_data.get('jats_ref_id')

                        # Нам нужен хотя бы какой-то идентификатор для ссылки (DOI, текст или внутренний ID)
                        if not (ref_doi_jats or ref_raw_text_jats or jats_ref_id):
                            continue

                        # Готовим данные для сохранения
                        ref_link_defaults = {
                            'raw_reference_text': ref_raw_text_jats,
                            'manual_data_json': {
                                'jats_ref_id': jats_ref_id,
                                'title': ref_data.get('title'),
                                'year': ref_data.get('year'),
                                'authors_str': ref_data.get('authors_str'),
                                'journal_title': ref_data.get('journal_title'),
                                'doi_from_source': ref_doi_jats,
                            }
                        }
                        # Удаляем None значения из manual_data_json
                        ref_link_defaults['manual_data_json'] = {k: v for k, v in ref_link_defaults['manual_data_json'].items() if v is not None}

                        if not ref_link_defaults['manual_data_json']:
                            ref_link_defaults['manual_data_json'] = None

                        # Определяем параметры для поиска существующей ссылки (чтобы избежать дублей)
                        lookup_params = {'source_article': article}
                        if ref_doi_jats:
                            lookup_params['target_article_doi'] = ref_doi_jats
                        # Если нет DOI, но есть jats_ref_id, можно искать по нему (требует поддержки БД для JSON-поиска)
                        # Для PostgreSQL можно так:
                        elif jats_ref_id:
                            lookup_params['manual_data_json__jats_ref_id'] = jats_ref_id
                        # Если нет ни DOI, ни jats_ref_id, используем сырой текст как последний вариант
                        elif ref_raw_text_jats:
                            lookup_params['raw_reference_text'] = ref_raw_text_jats
                        else:
                            continue # Пропускаем, если не за что зацепиться

                        # Устанавливаем статус и DOI в defaults
                        if ref_doi_jats:
                            ref_link_defaults['target_article_doi'] = ref_doi_jats
                            ref_link_defaults['status'] = ReferenceLink.StatusChoices.DOI_PROVIDED_NEEDS_LOOKUP
                        else:
                            ref_link_defaults['status'] = ReferenceLink.StatusChoices.PENDING_DOI_INPUT

                        # Создаем или обновляем объект ReferenceLink
                        ref_obj, ref_created = ReferenceLink.objects.update_or_create(
                            **lookup_params,
                            defaults=ref_link_defaults
                        )
                        processed_jats_ref_count += 1

                        # Ищем DOI для ссылки на crossref
                        if not ref_doi_jats:
                            # task = find_doi_for_reference_task.delay(
                            #     reference_link_id=ref_obj.id,
                            #     user_id=user_id
                            # )

                            # Формируем поисковый запрос для CrossRef
                            # Используем данные из manual_data_json (если есть название, авторы, год) или raw_reference_text
                            search_query_parts = []
                            if ref_obj.manual_data_json:
                                if ref_obj.manual_data_json.get('title'):
                                    search_query_parts.append(str(ref_obj.manual_data_json['title']))
                                # Можно добавить авторов, если они есть в structured виде, например, первый автор
                                if ref_obj.manual_data_json.get('authors_str'):
                                    search_query_parts.append(str(ref_obj.manual_data_json['authors_str']))
                                if ref_obj.manual_data_json.get('year'):
                                    search_query_parts.append(str(ref_obj.manual_data_json['year']))

                            if not search_query_parts and ref_obj.raw_reference_text: # Если нет структурированных данных, берем сырой текст
                                search_query_parts.append(ref_obj.raw_reference_text[:300]) # Ограничим длину запроса

                            if not search_query_parts:
                                send_user_notification(
                                    user_id,
                                    task_id,
                                    query_display_name,
                                    'INFO',
                                    f'{current_api_name} JATS: Insufficient data to find DOI for reference: {ref_obj.id}.',
                                    source_api=current_api_name,
                                    # originating_reference_link_id=reference_link_id
                                )
                                ref_obj.status = ReferenceLink.StatusChoices.ERROR_DOI_LOOKUP
                                ref_obj.save(update_fields=['status', 'updated_at'])

                            else:
                                bibliographic_query = " ".join(search_query_parts)

                                params = {
                                    'query.bibliographic': bibliographic_query,
                                    'rows': 1, # Нам нужен только самый релевантный результат
                                    'mailto': APP_EMAIL
                                }
                                print(f'######### params: {params}')
                                api_url = "https://api.crossref.org/works"
                                # headers = {'User-Agent': f'ScientificPapersApp/1.0 (mailto:{APP_EMAIL})'}
                                headers = {'User-Agent': USER_AGENT_LIST[0]}

                                ref_json_data = None
                                try:
                                    time.sleep(2)
                                    send_user_notification(
                                        user_id,
                                        task_id,
                                        query_display_name,
                                        'PROGRESS',
                                        f'{current_api_name} CrossRef query for DOI search:"{bibliographic_query[:50]}..."',
                                        # progress_percent=30,
                                        source_api=current_api_name,
                                        # originating_reference_link_id=reference_link_id
                                    )
                                    response = requests.get(api_url, params=params, headers=headers, timeout=30)
                                    # response.raise_for_status()
                                    if response and response.status_code == 200:
                                        ref_json_data = response.json()
                                        print(f'######## ref_json_data: {ref_json_data}')
                                except requests.exceptions.RequestException as exc:
                                    send_user_notification(
                                        user_id,
                                        task_id,
                                        query_display_name,
                                        'RETRYING',
                                        f'Ошибка сети/API CrossRef при поиске DOI: {str(exc)}',
                                        source_api=current_api_name,
                                        # originating_reference_link_id=reference_link_id
                                    )

                                if ref_json_data and ref_json_data.get('message') and ref_json_data['message'].get('items'):
                                    found_item = ref_json_data['message']['items'][0]
                                    found_doi = found_item.get('DOI')
                                    score = found_item.get('score', 0) # CrossRef возвращает score релевантности

                                    if found_doi:
                                        print(f'########### found_doi: {found_doi}')
                                        ref_doi_jats = found_doi
                                        # Можно добавить проверку score, чтобы отсеять совсем нерелевантные результаты
                                        # Например, if score > некоторого порога (например 60-70)
                                        ref_obj.target_article_doi = found_doi.lower()
                                        ref_obj.status = ReferenceLink.StatusChoices.DOI_PROVIDED_NEEDS_LOOKUP
                                        # Можно сохранить и другую информацию, например, название найденной статьи, в manual_data_json для сверки
                                        if 'title' in found_item and isinstance(found_item['title'], list) and found_item['title']:
                                            ref_obj.manual_data_json = ref_obj.manual_data_json or {}
                                            ref_obj.manual_data_json['found_title_by_doi_search'] = found_item['title'][0]
                                            ref_obj.manual_data_json['found_doi_score'] = score

                                        ref_obj.save(update_fields=['target_article_doi', 'status', 'manual_data_json', 'updated_at'])

                                        send_user_notification(
                                            user_id,
                                            task_id,
                                            query_display_name,
                                            'SUCCESS',
                                            f'Found DOI: {found_doi} (score: {score}) for reference.',
                                            # progress_percent=100,
                                            source_api=current_api_name,
                                            # originating_reference_link_id=reference_link_id
                                        )
                                    else:
                                        ref_obj.status = ReferenceLink.StatusChoices.ERROR_DOI_LOOKUP # Или новый статус "DOI не найден"
                                        ref_obj.save(update_fields=['status', 'updated_at'])
                                        send_user_notification(
                                            user_id,
                                            task_id,
                                            query_display_name,
                                            'FAILURE',
                                            'DOI not found in CrossRef response.',
                                            # progress_percent=100,
                                            source_api=current_api_name,
                                            # originating_reference_link_id=reference_link_id
                                        )
                                else:
                                    ref_obj.status = ReferenceLink.StatusChoices.ERROR_DOI_LOOKUP # Или новый статус "DOI не найден"
                                    ref_obj.save(update_fields=['status', 'updated_at'])
                                    send_user_notification(
                                        user_id,
                                        task_id,
                                        query_display_name,
                                        'FAILURE',
                                        'DOI not found (empty response from CrossRef).',
                                        # progress_percent=100,
                                        source_api=current_api_name,
                                        # originating_reference_link_id=reference_link_id
                                    )

                        # Если у ссылки есть DOI, ставим в очередь на обработку
                        if ref_doi_jats:
                            print(f"*** DEBUG (process_data_task) {current_api_name} ref_doi_jats: {ref_doi_jats}")
                            send_user_notification(
                                user_id,
                                task_id,
                                query_display_name,
                                'INFO',
                                f'{current_api_name} JATS: Found reference {ref_obj.id} with DOI: {ref_doi_jats}. Starting the pipeline.',
                                source_api=current_api_name
                            )
                            process_article_pipeline_task.delay(
                                identifier_value=ref_doi_jats,
                                identifier_type='DOI',
                                user_id=user_id,
                                originating_reference_link_id=ref_obj.id # Передаем ID созданной/обновленной ссылки
                            )

                    if processed_jats_ref_count > 0:
                        send_user_notification(
                            user_id,
                            task_id,
                            query_display_name,
                            'PROGRESS',
                            f'{current_api_name} JATS: Processed {processed_jats_ref_count} links.',
                            progress_percent=90,
                            source_api=current_api_name
                        )

        if hasattr(article, 'pmc_id') and article.pmc_id and not article.pdf_file:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                f'PMCID found: {article.pmc_id}. Home of the resulting PDF file...',
                progress_percent=55,
                source_api=current_api_name
            )
            try:
                time.sleep(2)
                pdf_file_name = f"article_{article.pmc_id}_{timezone.now().strftime('%Y%m%d%H%M%S')}.pdf"
                simple_pdf_url = f'https://pmc.ncbi.nlm.nih.gov/articles/{article.pmc_id}/pdf/'
                pdf_to_save, pdf_url = download_pdf_from_pmc(simple_pdf_url)
                if pdf_to_save:
                    print('*** DEBUG (process_data_task) if pdf_to_save:')
                    # print(pdf_to_save)
                    current_api_name = settings.API_SOURCE_NAMES['PUBMED']
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'INFO',
                        'PDF file successfully received from PMC.',
                        source_api=current_api_name
                    )
                else:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'WARNING',
                        f'Failed to retrieve PDF from PMC for {article.pmc_id}.',
                        source_api=current_api_name
                    )
            except Exception as exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'WARNING',
                    f'Error when requesting PDF file from PMC: {exc}',
                    source_api=current_api_name
                )

        if hasattr(article, 'doi') and article.doi and not article.pdf_file and not pdf_to_save:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                f'DOI: {article.doi}. Start of getting PDF file from Sci-Hub.box...',
                # progress_percent=55,
                source_api=current_api_name
            )
            try:
                time.sleep(2)
                pdf_file_name = f"article_{article.doi.replace('/', '_')}_{timezone.now().strftime('%Y%m%d%H%M%S')}.pdf"
                # doi_pdf_url = f'https://doi.org/{article.doi}'
                pdf_to_save, pdf_url = download_pdf_from_scihub_box(article.doi)
                if pdf_to_save:
                    current_api_name = settings.API_SOURCE_NAMES['SCIHUB']
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'INFO',
                        'PDF file successfully retrieved from Sci-Hub.Box',
                        source_api=current_api_name
                    )
                else:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'WARNING',
                        f'Failed to retrieve PDF from Sci-Hub.Box for {article.doi}.',
                        source_api=current_api_name
                    )
            except Exception as exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'WARNING',
                    f'Error when requesting a PDF file from Sci-Hub.Box: {exc}',
                    source_api=current_api_name
                )

            if hasattr(article, 'doi') and article.doi and not article.pdf_file and not pdf_to_save:
                if 'rxiv_version' in article_data and article_data['rxiv_version']:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'PROGRESS',
                        f'RXVI: Beginning to retrieve PDF file for DOI: {article.doi}...',
                        source_api=current_api_name
                    )
                    pdf_url, pdf_to_save = download_pdf_from_rxiv(article.doi, article_data['rxiv_version'])
                    if pdf_to_save:
                        current_api_name = settings.API_SOURCE_NAMES['RXIV']
                        send_user_notification(
                            user_id,
                            task_id,
                            query_display_name,
                            'INFO',
                            f'RXVI: PDF file for: {article.doi} was successfully retrieved from: {pdf_url}.',
                            source_api=current_api_name
                        )
                    else:
                        send_user_notification(
                            user_id,
                            task_id,
                            query_display_name,
                            'WARNING',
                            f'RXVI: Failed to retrieve PDF file for: {article.doi} from {pdf_url}.',
                            source_api=current_api_name
                        )

        if pdf_to_save and not article.pdf_file:
            try:
                article.pdf_file.save(pdf_file_name, ContentFile(pdf_to_save), save=False)
                article.pdf_url = pdf_url
                pdf_file_path = article.pdf_file.path
                if pdf_file_path:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'INFO',
                        f'MarkItDown: Start converting PDF file: {pdf_file_path} to text...',
                        source_api=current_api_name
                    )
                    # Конвертируем PDF в текст
                    with open(pdf_file_path, 'rb') as pdf_file_content_stream:
                        time.sleep(1)
                        extracted_markitdown_text = None
                        files = {'file': (os.path.basename(pdf_file_path), pdf_file_content_stream, 'application/pdf')}
                        markitdown_service_url = 'http://localhost:8181/convert-document/' # docker markitdown
                        response = requests.post(markitdown_service_url, files=files, timeout=310)
                        response.raise_for_status()
                        data = response.json()
                        extracted_markitdown_text = data.get("markdown_text")
                        # source_version_info = data.get("source_tool", "markitdown_service_1.0")
                        if extracted_markitdown_text:
                            send_user_notification(
                                user_id,
                                task_id,
                                query_display_name,
                                'SUCCESS',
                                f'MarkItDown for PDF file: {pdf_file_path} long: {len(extracted_markitdown_text)}.',
                                source_api=current_api_name
                            )
                            article.pdf_text = extracted_markitdown_text
                            if not article.structured_content:
                                article.primary_source_api = current_api_name
                        else:
                            send_user_notification(
                                user_id,
                                task_id,
                                query_display_name,
                                'INFO',
                                f'MarkItDown for PDF file: {pdf_file_path} did not return text. The answer is {data}',
                                source_api=current_api_name
                            )
            except requests.exceptions.RequestException as exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'RETRYING',
                    f'MarkItDown for PDF file: {pdf_file_path}. Network/API error: {str(exc)}. Repeat...',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
            except Exception as err:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'MarkItDown for PDF file: {pdf_file_path}. Error: {str(err)}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )

        article.save()

        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'PROGRESS',
            f'Article id {article.id} has been saved.',
            # progress_percent=50,
            # source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )

        print(f'*** DEBUG (process_data_task) author_list: {author_list}')
        if author_list:
            for author in author_list:
                author_obj = None
                author_orcid = None
                if author['full_name']:
                    try:
                        author_obj, author_created = Author.objects.update_or_create(
                            full_name=author['full_name'].strip().replace('.', ''),
                            defaults={
                                'first_name': author['first_name'],
                                'last_name': author['last_name'],
                                'affiliation': author['affiliations'],
                            }
                        )
                        print(f'********* author_created: {author_created}, author_obj: {author_obj}')
                        if author_obj:
                            if author['last_name'] and (article.doi or article.pubmed_id):
                                find_orcid_result = find_orcid(author['last_name'], article.doi, article.pubmed_id)
                                author_orcid = find_orcid_result.get('orcid_id')
                                print(f'********** ORCID ID: {author_orcid}')
                                if author_orcid:
                                    if not Author.objects.filter(orcid=author_orcid):
                                        print('**** author orcid not exist')
                                        if not author_obj.orcid:
                                            author_obj.orcid = author_orcid
                                            author_obj.save()
                            ArticleAuthor.objects.get_or_create(article=article, author=author_obj)
                    except Exception as e_author:
                        send_user_notification(
                            user_id,
                            task_id,
                            query_display_name,
                            'ERROR',
                            f'Error creating/updating Author: {str(e_author)}',
                            source_api=current_api_name,
                            originating_reference_link_id=originating_reference_link_id
                        )

        # обновление ReferenceLink, если originating_reference_link_id
        if originating_reference_link_id and article:
            try:
                ref_link = ReferenceLink.objects.get(id=originating_reference_link_id, source_article__user=article_owner)
                ref_link.resolved_article = article
                ref_link.status = ReferenceLink.StatusChoices.ARTICLE_LINKED
                ref_link.save(update_fields=['resolved_article', 'status', 'updated_at'])
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'INFO',
                    f'The link ID {ref_link.id} is associated with the article.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
            except ReferenceLink.DoesNotExist:
                pass
            except Exception as e_ref:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'ERROR',
                    f'Ref_link update error: {str(e_ref)}',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )

        # Если был получен полный текст, запускаем задачу для его анализа и связывания
        if article.is_user_initiated and (full_text_xml_pmc or full_text_xml_rxvi or full_text_xml_europepmc):
            print('*** DEBUG (process_data_task) if article.is_user_initiated and (full_text_xml_pmc or full_text_xml_rxvi or full_text_xml_europepmc):')
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'INFO',
                'The task is set to automatically link text and links.',
                source_api=current_api_name
            )
            process_full_text_and_create_segments_task.delay(
                article_id=article.id,
                user_id=user_id
            )

        # финальное уведомление
        final_message = f'{query_display_name}: Данные Статьи {article.id} "{article.title[:30]}...".'
        if full_text_xml_pmc or full_text_xml_rxvi or full_text_xml_europepmc:
            final_message += " Full text received, segmentation started."
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'SUCCESS',
            final_message,
            progress_percent=100,
            article_id=article.id,
            # created=created,
            # source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )
        return {'status': 'success', 'message': final_message, 'doi': article.doi, 'article_id': article.id}
    except Exception as e:
        error_message = f'Error PROCESS_DATA_TASK: User ID {user_id}, Article ID {article_id}, DOI {article.doi}, Data length {len(article_data_list)}. Msg: {e}'
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'FAILURE',
            error_message,
            # source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id,
        )
        return {'status': 'error', 'message': error_message}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_data_from_crossref_task(
    self,
    doi: str,
    user_id: int = None,
    process_references: bool = False,
    originating_reference_link_id: int = None):
    # article_id_to_update: int = None): # Параметр для обновления существующей статьи

    try:
        task_id = self.request.id
        current_api_name = settings.API_SOURCE_NAMES['CROSSREF']
        query_display_name = f"DOI:{doi}"

        article_data = {
            # models.Article
            'user': None,
            'title': None,
            'authors': [],
            'abstract': None,
            'doi': None,
            'pubmed_id': None,
            'pmc_id': None,
            'arxiv_id': None,
            'cleaned_text_for_llm': None,
            'is_manually_added_full_text': False,
            'pdf_file': None,
            'pdf_text': None,
            'pdf_file_name': None, # если будет надо имя для pdf
            'primary_source_api': None,
            'publication_date': None,
            'journal_name': None,
            'oa_status': None,
            'best_oa_url': None,
            'best_oa_pdf_url': None,
            'oa_license': None,
            'is_user_initiated': False,
            'structured_content': {},
            # models.ArticleContent
            'current_api_name': None,
            'article_content_format_type': None,
            'article_content': {},
        }

        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'PENDING',
            f'Process {current_api_name} for {query_display_name}' + (' (including references)' if process_references else ''),
            progress_percent=0,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )

        if not doi:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                'DOI not specified.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'error', 'message': 'DOI not specified.', 'doi': query_display_name}

        headers = {'User-Agent': USER_AGENT_LIST[0]}
        # DOI в URL CrossRef обычно не чувствителен к регистру, но приведем к нижнему для единообразия
        safe_doi_url_part = requests.utils.quote(doi.lower()) # Кодируем DOI для URL
        api_url = f"https://api.crossref.org/works/{safe_doi_url_part}"

        try:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                f'Запрос: {api_url}',
                progress_percent=10,
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            response = requests.get(api_url, headers=headers, timeout=30)
            print(f'*** DEBUG (fetch_data_from_crossref_task) response: {response}')
            response.raise_for_status()
            data = response.json()
            api_data = data.get('message', {}) # основные данные от API
        except requests.exceptions.RequestException as exc:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'RETRYING',
                f'Network/API error {current_api_name}: {str(exc)}. Repeat...',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            raise self.retry(exc=exc)
        except json.JSONDecodeError as json_exc:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                f'JSON decoding error from {current_api_name}: {str(json_exc)}',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'error', 'message': f'{current_api_name} JSON Decode Error: {str(json_exc)}'}
        except Exception as err:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                f'Error from {current_api_name}: {str(err)}',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'error', 'message': f'{current_api_name} Error: {str(err)}'}

        if not api_data:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'NOT_FOUND',
                f'{current_api_name} API: response does not contain "message" or article not found.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {
                'status': 'not_found',
                'message': f'{current_api_name} API: no "message" in response or article not found.', 'doi': query_display_name, 'raw_response': data
            }

        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'PROGRESS',
            f'Data {current_api_name} received, processing...',
            progress_percent=25,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )

        # Используем DOI из ответа API как канонический, если он есть, иначе исходный DOI
        api_doi_from_response = api_data.get('DOI', doi).lower()

        api_title_list = api_data.get('title')
        extracted_api_title = ", ".join(api_title_list).strip() if api_title_list and isinstance(api_title_list, list) and api_title_list else None

        api_abstract_raw = api_data.get('abstract')
        extracted_api_abstract = api_abstract_raw.replace('<i>', '').replace('</i>', '').strip() if api_abstract_raw else None

        api_pub_date_data = api_data.get('published-print') or api_data.get('published-online') or api_data.get('created')
        extracted_api_parsed_date = None
        if api_pub_date_data and api_pub_date_data.get('date-parts') and isinstance(api_pub_date_data['date-parts'], list) and api_pub_date_data['date-parts'][0]:
            date_parts = api_pub_date_data['date-parts'][0]
            if isinstance(date_parts, list) and date_parts:
                try:
                    extracted_api_parsed_date = timezone.datetime(
                        year=int(date_parts[0]),
                        month=int(date_parts[1]) if len(date_parts) > 1 else 1,
                        day=int(date_parts[2]) if len(date_parts) > 2 else 1
                    ).date()
                except (ValueError, TypeError, IndexError):
                    pass

        api_journal_list = api_data.get('container-title')
        extracted_api_journal_name = ", ".join(api_journal_list).strip() if api_journal_list and isinstance(api_journal_list, list) and api_journal_list else None

        article_data['doi'] = api_data.get('DOI', doi).lower()
        if 'PMID' in api_data and api_data['PMID']:
            article_data['pubmed_id'] = str(api_data['PMID']) # Убедимся, что это строка
        if extracted_api_title:
            article_data['title'] = extracted_api_title
        if extracted_api_abstract:
            article_data['abstract'] = extracted_api_abstract
        if extracted_api_parsed_date:
            article_data['publication_date'] = extracted_api_parsed_date
        if extracted_api_journal_name:
            article_data['journal_name'] = extracted_api_journal_name
        article_data['current_api_name'] = current_api_name
        article_data['article_content_format_type'] = 'json_metadata'
        article_data['article_content'] = json.dumps(api_data)
        extracted_api_authors = api_data.get('author', [])
        if extracted_api_authors:
            article_data['authors'] = parse_crossref_authors(extracted_api_authors)

        final_message = f'Статья {current_api_name} "{article_data['title'][:30]}..." {"получена"}.'
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'SUCCESS',
            final_message,
            progress_percent=100,
            # article_id=article.id,
            # created=created,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )
        return {
            'status': 'success',
            'message': final_message,
            'doi': api_doi_from_response,
            # 'article_id': article.id
            'article_data': article_data,
        }

    except Exception as e:
        error_message_for_user = f'Internal error {current_api_name}: {type(e).__name__} - {str(e)}'
        self.update_state(
            state='FAILURE',
            meta={
                'identifier': query_display_name,
                'error': error_message_for_user,
                'traceback': self.request.exc_info if hasattr(self.request, 'exc_info') else str(e)
            }
        )
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'FAILURE',
            error_message_for_user,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )
        return {'status': 'error', 'message': error_message_for_user, 'doi': query_display_name}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_data_from_pubmed_task(
    self,
    identifier_value: str,
    identifier_type: str = 'PMID',
    user_id: int = None,
    originating_reference_link_id: int = None):
    # article_id_to_update: int = None):

    try:
        article_data = {
            # models.Article
            'user': None,
            'title': None,
            'authors': [],
            'abstract': None,
            'doi': None,
            'pubmed_id': None,
            'pmc_id': None,
            'arxiv_id': None,
            'cleaned_text_for_llm': None,
            'is_manually_added_full_text': False,
            'pdf_file': None,
            'pdf_text': None,
            'primary_source_api': None,
            'publication_date': None,
            'journal_name': None,
            'oa_status': None,
            'best_oa_url': None,
            'best_oa_pdf_url': None,
            'oa_license': None,
            'is_user_initiated': False,
            'structured_content': {},
            # models.ArticleContent
            'current_api_name': None,
            # 'article_content_format_type': None,
            # 'article_content': None,
            'article_contents': {},
        }
        task_id = self.request.id
        query_display_name = f"{identifier_type.upper()}:{identifier_value}"
        current_api_name = settings.API_SOURCE_NAMES['PUBMED']

        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'PENDING',
            f'Start processing {current_api_name}...',
            progress_percent=0,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id,
        )

        if not identifier_value:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                'Identifier not specified.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': 'Identifier not specified.'}

        article_owner = None
        if user_id:
            try:
                article_owner = User.objects.get(id=user_id)
            except User.DoesNotExist:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'User ID {user_id} not found.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                return {'status': 'error', 'message': f'User ID {user_id} not found.'}

        eutils_base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        common_params = {'tool': NCBI_TOOL_NAME, 'email': NCBI_ADMIN_EMAIL}
        if NCBI_API_KEY:
            common_params['api_key'] = NCBI_API_KEY

        pmid_to_fetch = None
        pmcid_to_fetch = None
        pmc_esearch_data = {}
        # original_doi = None

        if identifier_type.upper() == 'DOI':
            try:
                # if not NCBI_API_KEY: time.sleep(0.35)
                time.sleep(0.35)
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'PROGRESS',
                    f'Search for PMID by DOI {identifier_value} via ESearch....',
                    progress_percent=10,
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                pubmed_esearch_params = {**common_params, 'db': 'pubmed', 'term': f"{identifier_value}[DOI]", 'retmode': 'json'}
                esearch_response = requests.get(f"{eutils_base}esearch.fcgi", params=pubmed_esearch_params, timeout=30)
                print(f'*** DEBUG (fetch_data_from_pubmed_task) pubmed esearch_response: {esearch_response}, pubmed_esearch_params: {pubmed_esearch_params}')
                # esearch_response.raise_for_status()
                if esearch_response.status_code == 200:
                    pubmed_esearch_data = esearch_response.json()
                    print(f'*** DEBUG (fetch_data_from_pubmed_task) pubmed esearch_params: {pubmed_esearch_params}, \n esearch_data: {pubmed_esearch_data}')
                    # if esearch_data.get('esearchresult', {}).get('idlist') and len(esearch_data['esearchresult']['idlist']) > 0:
                    if 'esearchresult' in  pubmed_esearch_data and pubmed_esearch_data['esearchresult']:
                        if 'idlist' in pubmed_esearch_data['esearchresult'] and pubmed_esearch_data['esearchresult']['idlist']:
                            if len(pubmed_esearch_data['esearchresult']['idlist']) > 0:
                                pmid_to_fetch = pubmed_esearch_data['esearchresult']['idlist'][0]
                                query_display_name = f"PMID:{pmid_to_fetch} (found by DOI:{identifier_value})"
                                send_user_notification(
                                    user_id,
                                    task_id,
                                    query_display_name,
                                    'PROGRESS',
                                    f'PMID {pmid_to_fetch} found. Query EFetch...',
                                    # progress_percent=20,
                                    source_api=current_api_name,
                                    originating_reference_link_id=originating_reference_link_id
                                )
                else:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'NOT_FOUND',
                        'PMID not found for the specified DOI.',
                        source_api=current_api_name,
                        originating_reference_link_id=originating_reference_link_id
                    )
                    # return {'status': 'not_found', 'message': 'PMID not found for DOI.'}
            except requests.exceptions.RequestException as exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'RETRYING',
                    f'(PMID) ESearch error {current_api_name}: {str(exc)}. Repeat...',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                raise self.retry(exc=exc)
            except json.JSONDecodeError as json_exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'(PMID) Error decoding JSON from ESearch {current_api_name}: {str(json_exc)}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                return {'status': 'error', 'message': f'(PMID) ESearch JSON Decode Error: {str(json_exc)}'}
            except Exception as e:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'(PMID) Error from ESearch {current_api_name}: {e}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                return {'status': 'error', 'message': f'(PMID) ESearch Error: {e}'}

            try:
                time.sleep(0.35)
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'PROGRESS',
                    f'Search PMCID for DOI {identifier_value} via ESearch...',
                    progress_percent=15,
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                pmc_esearch_params = {**common_params, 'db': 'pmc', 'term': f"{identifier_value}[DOI]", 'retmode': 'json'}
                pmc_esearch_response = requests.get(f"{eutils_base}esearch.fcgi", params=pmc_esearch_params, timeout=30)
                print(f'*** DEBUG (fetch_data_from_pubmed_task) pmc_esearch_response: {pmc_esearch_response}')
                # pmc_esearch_response.raise_for_status()
                if esearch_response.status_code == 200:
                    pmc_esearch_data = pmc_esearch_response.json()
                    print(f'*** DEBUG (fetch_data_from_pubmed_task) pmc_esearch_data: {pmc_esearch_data}')
                    # if pmc_esearch_data.get('esearchresult', {}).get('idlist') and len(pmc_esearch_data['esearchresult']['idlist']) > 0:
                    if 'esearchresult' in  pmc_esearch_data and pmc_esearch_data['esearchresult']:
                        if 'idlist' in pmc_esearch_data['esearchresult'] and pmc_esearch_data['esearchresult']['idlist']:
                            if len(pmc_esearch_data['esearchresult']['idlist']) > 0:
                                pmcid_to_fetch = pmc_esearch_data['esearchresult']['idlist'][0]
                                query_display_name = f"PMCID:{pmcid_to_fetch} (found by DOI:{identifier_value})"
                                send_user_notification(
                                    user_id,
                                    task_id,
                                    query_display_name,
                                    'PROGRESS',
                                    f'PMCID {pmcid_to_fetch} found. Query EFetch...',
                                    # progress_percent=20,
                                    source_api=current_api_name,
                                    originating_reference_link_id=originating_reference_link_id
                                )
                else:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'NOT_FOUND',
                        'PMCID not found for the specified DOI.',
                        source_api=current_api_name,
                        originating_reference_link_id=originating_reference_link_id
                    )
            except requests.exceptions.RequestException as exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'RETRYING',
                    f'(PMCID) ESearch error {current_api_name}: {str(exc)}. Repeat..',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                raise self.retry(exc=exc)
            except json.JSONDecodeError as json_exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'(PMCID) ESearch JSON decoding error {current_api_name}: {str(json_exc)}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                return {'status': 'error', 'message': f'(PMCID) ESearch JSON Decode Error: {str(json_exc)}'}
            except Exception as e:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'(PMCID) Error from ESearch {current_api_name}: {e}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                return {'status': 'error', 'message': f'(PMCID) ESearch Error: {e}'}
        elif identifier_type.upper() == 'PMID':
            pmid_to_fetch = identifier_value
            if pmid_to_fetch and not pmcid_to_fetch:
                pmc_esearch_from_pmid_params = {**common_params, 'db': 'pmc', 'term': f"{pmid_to_fetch}[PMID]", 'retmode': 'json'}
                try:
                    time.sleep(0.35)
                    pmc_esearch_from_pmid_response = requests.get(
                        f"{eutils_base}esearch.fcgi", params=pmc_esearch_from_pmid_params, timeout=30
                    )
                    print(f'*** DEBUG (fetch_data_from_pubmed_task) pmc_esearch_from_pmid_response: {pmc_esearch_from_pmid_response}')
                    # pmc_esearch_from_pmid_response.raise_for_status()
                    if pmc_esearch_from_pmid_response.status_code == 200:
                        pmc_esearch_from_pmid_data = pmc_esearch_from_pmid_response.json()
                        print(f'*** DEBUG (fetch_data_from_pubmed_task) pmc_esearch_from_pmid_data: {pmc_esearch_from_pmid_data}')
                        # if pmc_esearch_from_pmid_data.get('esearchresult', {}).get('idlist') and len(pmc_esearch_from_pmid_data['esearchresult']['idlist']) > 0:
                        if 'esearchresult' in pmc_esearch_from_pmid_data and pmc_esearch_from_pmid_data['esearchresult']:
                            if 'idlist' in pmc_esearch_from_pmid_data['esearchresult'] and pmc_esearch_from_pmid_data['esearchresult']['idlist']:
                                if len(pmc_esearch_from_pmid_data['esearchresult']['idlist']) > 0:
                                    pmcid_to_fetch = pmc_esearch_from_pmid_data['esearchresult']['idlist'][0]
                                    query_display_name = f"PMCID:{pmcid_to_fetch} (found at DOI:{identifier_value})"
                                    send_user_notification(
                                        user_id,
                                        task_id,
                                        query_display_name,
                                        'PROGRESS',
                                        f'PMCID {pmcid_to_fetch} found. Query EFetch...',
                                        progress_percent=20,
                                        source_api=current_api_name,
                                        originating_reference_link_id=originating_reference_link_id
                                    )
                except requests.exceptions.RequestException as exc:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'RETRYING',
                        f'ESearch error {current_api_name}: {str(exc)}. Повтор...',
                        source_api=current_api_name,
                        originating_reference_link_id=originating_reference_link_id
                    )
                    raise self.retry(exc=exc)
                except json.JSONDecodeError as json_exc:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'FAILURE',
                        f'Error decoding JSON from ESearch {current_api_name}: {str(json_exc)}.',
                        source_api=current_api_name,
                        originating_reference_link_id=originating_reference_link_id
                    )
                    return {'status': 'error', 'message': f'(PMCID) ESearch JSON Decode Error: {str(json_exc)}'}
                except Exception as e:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'FAILURE',
                        f'(PMCID) Error from ESearch {current_api_name}: {e}.',
                        source_api=current_api_name,
                        originating_reference_link_id=originating_reference_link_id
                    )
                    return {'status': 'error', 'message': f'(PMCID) ESearch Error: {e}'}
        else:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                f'Unsupported identifier type for {current_api_name}: {identifier_type}.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'error', 'message': f'Unsupported identifier type for {current_api_name}: {identifier_type}.'}

        if not pmid_to_fetch and not pmcid_to_fetch:
            # ESearch не нашёл ни PMID ни PMCID
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'NOT_FOUND',
                'PMID not found for the specified DOI.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'not_found', 'message': 'PMID and PMCID not found for DOI.'}

        # Обновляем имя для отображения
        # if original_doi:
        #     query_display_name = f"PMID:{pmid_to_fetch} (из DOI:{original_doi})"
        if pmid_to_fetch and pmcid_to_fetch:
            query_display_name = f"PMID:{pmid_to_fetch} PMCID:{pmcid_to_fetch} (из DOI:{identifier_value})"
        elif pmid_to_fetch:
            query_display_name = f"PMID:{pmid_to_fetch} (из DOI:{identifier_value})"
        elif pmcid_to_fetch:
            query_display_name = f"PMCID:{pmcid_to_fetch} (из DOI:{identifier_value})"

        # Запрос метаданных и PMCID из db=pubmed
        efetch_params = {**common_params, 'db': 'pubmed', 'id': pmid_to_fetch, 'retmode': 'xml', 'rettype': 'abstract'}
        xml_content_pubmed = None
        try:
            # if not NCBI_API_KEY: time.sleep(0.35)
            time.sleep(0.35)
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                'Querying metadata from PubMed...',
                progress_percent=25,
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            efetch_response = requests.get(f"{eutils_base}efetch.fcgi", params=efetch_params, timeout=45)
            print(f'*** DEBUG (fetch_data_from_pubmed_task) efetch_response: {efetch_response}')
            # efetch_response.raise_for_status()
            if efetch_response.status_code == 200:
                xml_content_pubmed = efetch_response.text
        except Exception as exc:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'RETRYING',
                f'Fetch error (db=pubmed): {exc}. Repeat....',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            raise self.retry(exc=exc)

        # Парсинг метаданных из ответа 'pubmed'
        api_title, api_abstract, api_journal_title, api_doi, api_pmcid = None, None, None, None, None
        api_parsed_authors, api_mesh_terms = [], []
        api_parsed_publication_date = None

        try:
            root = ET.fromstring(xml_content_pubmed)
            pubmed_article_node = root.find('.//PubmedArticle')
            if pubmed_article_node is None:
                raise ValueError("Structure PubmedArticle not found in XML.")

            article_node = pubmed_article_node.find('.//Article')
            if article_node is None:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'NOT_FOUND',
                    'Article tag not found in PubmedArticle.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                # return {'status': 'not_found', 'message': 'Article tag not found in PubmedArticle.'}
                raise ValueError("Article tag not found in PubmedArticle.")

            title_el = article_node.find('.//ArticleTitle')
            api_title = title_el.text.strip() if title_el is not None and title_el.text else None

            abstract_text_parts = []
            for abst_text_node in article_node.findall('.//AbstractText'):
                if abst_text_node.text:
                    label = abst_text_node.get('Label')
                    if label:
                        abstract_text_parts.append(f"{label.upper()}: {abst_text_node.text.strip()}")
                    else:
                        abstract_text_parts.append(abst_text_node.text.strip())
            api_abstract = "\n\n".join(abstract_text_parts) if abstract_text_parts else None # Разделяем параграфы абстракта

            # api_parsed_authors = parse_pubmed_authors(article_node.find('.//AuthorList'))
            api_parsed_authors = parse_pubmed_authors_from_xml_metadata(article_node.find('.//AuthorList'))

            journal_node = article_node.find('.//Journal')
            if journal_node is not None:
                journal_title_el = journal_node.find('./Title')
                if journal_title_el is not None and journal_title_el.text:
                    api_journal_title = journal_title_el.text.strip()

            pubdate_node = article_node.find('.//Journal/JournalIssue/PubDate')
            if pubdate_node is not None:
                year_el, month_el, day_el = pubdate_node.find('./Year'), pubdate_node.find('./Month'), pubdate_node.find('./Day')
                if year_el is not None and year_el.text:
                    try:
                        year = int(year_el.text)
                        month_str = month_el.text if month_el is not None and month_el.text else "1"
                        day_str = day_el.text if day_el is not None and day_el.text else "1"
                        month_map = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
                        month = int(month_str) if month_str.isdigit() else month_map.get(month_str.lower()[:3], 1)
                        api_parsed_publication_date = timezone.datetime(year, month, int(day_str)).date()
                    except (ValueError, TypeError):
                        pass

            # ВАЖНО: ищем PMCID именно для основной статьи
            pmcid_node = pubmed_article_node.find(".//PubmedData/ArticleIdList/ArticleId[@IdType='pmc']")
            if pmcid_node is not None and pmcid_node.text:
                api_pmcid = pmcid_node.text.strip() # Получаем полный PMCID, например "PMC1234567"

            doi_node = pubmed_article_node.find(".//ArticleIdList/ArticleId[@IdType='doi']")
            if doi_node is not None and doi_node.text:
                api_doi = doi_node.text.strip().lower()

            mesh_list_node = pubmed_article_node.find('.//MeshHeadingList')
            if mesh_list_node is not None:
                for mesh_node in mesh_list_node.findall('./MeshHeading'):
                    desc_el = mesh_node.find('./DescriptorName')
                    if desc_el is not None and desc_el.text:
                        api_mesh_terms.append(desc_el.text.strip())
        except Exception as e_xml:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                f'PubMed XML parsing error: {e_xml}',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'error', 'message': f'Error parsing PubMed XML: {e_xml}'}

        # --- ПОЛУЧЕНИЕ ПОЛНОГО ТЕКСТА ИЗ PMC (db=pmc), ЕСЛИ ЕСТЬ PMCID ---
        if not api_pmcid and pmcid_to_fetch:
            api_pmcid = pmcid_to_fetch

        full_text_xml_pmc = None
        pdf_file_name = None
        pmc_pdf_url = None
        # pdf_to_save = None

        if api_pmcid:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                f'PMCID found: {api_pmcid}. Full text request...',
                progress_percent=50,
                source_api=current_api_name
            )
            api_pmcid = api_pmcid if api_pmcid.upper().startswith('PMC') else f"PMC{api_pmcid}"
            pmc_efetch_params = {**common_params, 'db': 'pmc', 'id': api_pmcid, 'retmode': 'xml'} # rettype не нужен, вернет полный XML
            try:
                # if not NCBI_API_KEY: time.sleep(0.35)
                time.sleep(0.35)
                pmc_response = requests.get(f"{eutils_base}efetch.fcgi", params=pmc_efetch_params, timeout=90)
                if pmc_response.status_code == 200:
                    full_text_xml_pmc = pmc_response.text
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'INFO',
                        'The full text of the JATS XML was successfully retrieved from PMC.',
                        source_api=current_api_name
                    )
                else:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'WARNING',
                        f'Failed to retrieve full text from PMC for {api_pmcid} (status: {pmc_response.status_code}).',
                        source_api=current_api_name
                    )
            except Exception as exc:
                send_user_notification(user_id, task_id, query_display_name, 'WARNING', f'Error when requesting full text from PMC: {exc}', source_api=current_api_name)

            pdf_file_name = f"article_{api_pmcid}_{timezone.now().strftime('%Y%m%d%H%M%S')}.pdf"
            pmc_pdf_url = f'https://pmc.ncbi.nlm.nih.gov/articles/{api_pmcid}/pdf/'

        if api_title:
            article_data['title'] = api_title
        # if article_authors:
        #     article_data['authors'] = article_authors
        if api_parsed_authors:
            article_data['authors'] = api_parsed_authors
        if api_abstract:
            article_data['abstract'] = api_abstract
        if api_doi:
            article_data['doi'] = api_doi
        if pmid_to_fetch:
            article_data['pubmed_id'] = pmid_to_fetch
        if api_pmcid:
            article_data['pmc_id'] = api_pmcid
        if api_journal_title:
            article_data['journal_name'] = api_journal_title
        if api_parsed_publication_date:
            article_data['publication_date'] = api_parsed_publication_date

        # if pdf_to_save:
        #     article_data['pdf_file'] = pdf_to_save
        if pmc_pdf_url:
            article_data['pmc_pdf_url'] = pmc_pdf_url
            article_data['identifier_value_for_download_pdf'] = identifier_value
        if pdf_file_name:
            article_data['pdf_file_name'] = pdf_file_name

        article_data['current_api_name'] = current_api_name

        if full_text_xml_pmc:
            article_data['article_contents']['full_text_xml_pmc'] = full_text_xml_pmc # json.dumps(full_text_xml_pmc)
        if xml_content_pubmed:
            article_data['article_contents']['xml_pubmed_entry'] = xml_content_pubmed # json.dumps(xml_content_pubmed)
        if api_mesh_terms:
            article_data['article_contents']['mesh_terms'] = api_mesh_terms # json.dumps(api_mesh_terms)

        final_message = f'Статья {current_api_name} "{article_data['title'][:30]}..." {"получена"}.'
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'SUCCESS',
            final_message,
            progress_percent=100,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )
        return {
            'status': 'success',
            'message': final_message,
            'identifier': query_display_name,
            'article_data': article_data,
        }

    except Exception as e:
        error_message_for_user = f'Внутренняя ошибка {current_api_name}: {type(e).__name__} - {str(e)}'
        self.update_state(
            state='FAILURE',
            meta={
                'identifier': query_display_name,
                'error': error_message_for_user,
                'traceback': self.request.exc_info if hasattr(self.request, 'exc_info') else str(e)
            }
        )
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'FAILURE',
            error_message_for_user,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )
        return {'status': 'error', 'message': error_message_for_user, 'identifier': query_display_name}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_data_from_europepmc_task(
    self,
    identifier_value: str,
    identifier_type: str = 'DOI',
    user_id: int = None,
    originating_reference_link_id: int = None):
    # article_id_to_update: int = None):

    try:
        task_id = self.request.id
        query_display_name = f"{identifier_type.upper()}:{identifier_value}"
        current_api_name = settings.API_SOURCE_NAMES['EUROPEPMC']
        article_data = {
            'article_contents': {}
        }

        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'PENDING',
            f'Start processing {current_api_name}...',
            progress_percent=0,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id,
        )

        if not identifier_value:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                'Identifier not specified.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': 'Identifier not specified.', 'identifier': query_display_name}

        article_owner = None
        if user_id:
            try:
                article_owner = User.objects.get(id=user_id)
            except User.DoesNotExist:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'User ID {user_id} not found.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id,
                )
                return {'status': 'error', 'message': f'User with ID {user_id} not found.'}

        # Запрос к API поиска для получения метаданных и PMCID
        query_string = f"{identifier_type.upper()}:{identifier_value}"
        search_params = {'query': query_string, 'format': 'json', 'resultType': 'core', 'email': APP_EMAIL}
        api_search_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

        try:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                f'Request for {api_search_url}',
                progress_percent=20,
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            response = requests.get(api_search_url, params=search_params, timeout=45)
            # response.raise_for_status()
            if response and response.status_code == 200:
                data = response.json()
            else:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'NOT_FOUND',
                    f'EUROPEPMC was unable to retrieve data for {query_string}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id,
                )
        except requests.exceptions.RequestException as exc:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'RETRYING',
                f'Ошибка сети/API {current_api_name}: {str(exc)}. Повтор...',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            raise self.retry(exc=exc)
        except json.JSONDecodeError as json_exc:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                f'JSON decoding error from {current_api_name}: {str(json_exc)}',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'error', 'message': f'{current_api_name} JSON Decode Error: {str(json_exc)}'}

        if not data or not data.get('resultList') or not data['resultList'].get('result'):
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'NOT_FOUND',
                'Article not found in Europe PMC.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id,
            )
            return {'status': 'not_found', 'message': 'Article not found in Europe PMC.', 'identifier': query_display_name}

        api_article_data = data['resultList']['result'][0]
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'PROGRESS',
            'Metadata received, processing...',
            progress_percent=40,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id,
        )

        # Извлечение данных из ответа API EPMC
        api_title = api_article_data.get('title')
        api_abstract = api_article_data.get('abstractText')
        api_doi = api_article_data.get('doi')
        if api_doi:
            api_doi = api_doi.lower()
        api_pmid = api_article_data.get('pmid')
        api_pmcid = api_article_data.get('pmcid')

        api_pub_date_str = api_article_data.get('firstPublicationDate')
        api_parsed_date = None
        if api_pub_date_str:
            try:
                api_parsed_date = timezone.datetime.strptime(api_pub_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        api_journal_name = None
        journal_info = api_article_data.get('journalInfo')
        if journal_info and isinstance(journal_info, dict):
            journal_details = journal_info.get('journal')
            if journal_details and isinstance(journal_details, dict) and journal_details.get('title'):
                api_journal_name = journal_details['title']

        api_parsed_authors = parse_europepmc_authors(api_article_data.get('authorList', {}).get('author'))

        # --- Загрузка полного текста по PMCID, если он есть ---
        full_text_xml_content = None
        if api_pmcid: # api_pmcid извлекается из api_article_data
            # PMCID в Europe PMC API используется с префиксом 'PMC'
            pmcid_for_url = api_pmcid if api_pmcid.upper().startswith('PMC') else f"PMC{api_pmcid}"
            full_text_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid_for_url}/fullTextXML"
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                f'PMCID found: {pmcid_for_url}. Full text query...',
                progress_percent=50,
                source_api=current_api_name,
            )
            try:
                # headers={'User-Agent': f'ScientificPapersApp/1.0 ({APP_EMAIL})'}
                headers = {'User-Agent': USER_AGENT_LIST[0]}
                full_text_response = requests.get(full_text_url, timeout=90, headers=headers)
                if full_text_response.status_code == 200:
                    full_text_xml_content = full_text_response.text
                    # print(f'******* EUROPEPMC**** pmcid_for_url: {pmcid_for_url}, full_text_xml_content: {full_text_xml_content}')
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'INFO',
                        'The full text of the JATS XML was successfully retrieved from the Europe PMC.',
                        source_api=current_api_name,
                    )
                else:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'WARNING',
                        f'Failed to retrieve full text from EuropePMC for {pmcid_for_url} (status: {full_text_response.status_code})',
                        source_api=current_api_name,
                    )
            except Exception as exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'WARNING',
                    f'Error when requesting full text from Europe PMC: {exc}',
                    source_api=current_api_name,
                )


        article_data['current_api_name'] = current_api_name

        if api_title:
            article_data['title'] = api_title
        if api_abstract:
            article_data['abstract'] = api_abstract
        if api_parsed_date:
            article_data['publication_date'] = api_parsed_date
        if api_journal_name:
            article_data['journal_name'] = api_journal_name
        if api_doi:
            article_data['doi'] = api_doi
        if api_pmid:
            article_data['pubmed_id'] = api_pmid
        if api_parsed_authors:
            article_data['authors'] = api_parsed_authors

        if full_text_xml_content:
            article_data['article_contents']['full_text_xml_europepmc'] = full_text_xml_content # json.dumps(full_text_xml_pmc)

        final_message = f'The article {current_api_name} "{article_data['title'][:30]}..." has been retrieved.'
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'SUCCESS',
            final_message,
            progress_percent=100,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id,
        )
        return {
            'status': 'success',
            'message': final_message,
            'identifier': query_display_name,
            'article_data': article_data,
        }
    except Exception as e:
        error_message_for_user = f'Internal error {current_api_name}: {type(e).__name__} - {str(e)}'
        self.update_state(
            state='FAILURE',
            meta={
                'identifier': query_display_name,
                'error': error_message_for_user,
                'traceback': self.request.exc_info if hasattr(self.request, 'exc_info') else str(e)
            }
        )
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'FAILURE',
            error_message_for_user,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id,
        )
        return {'status': 'error', 'message': f'Internal error: {str(e)}', 'identifier': query_display_name}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_data_from_rxiv_task(
    self,
    doi: str,
    user_id: int = None,
    originating_reference_link_id: int = None):
    # article_id_to_update: int = None):
    try:
        article_data = {}
        task_id = self.request.id
        clean_doi_for_query = doi.replace('DOI:', '').strip().lower()
        query_display_name = f"DOI:{clean_doi_for_query} (Rxiv)"
        current_api_name = settings.API_SOURCE_NAMES['RXIV']

        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'PENDING',
            f'Start processing {current_api_name}...',
            progress_percent=0,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )

        if not clean_doi_for_query:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'FAILURE',
                'DOI not specified.',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'error', 'message': 'DOI не указан.'}

        # --- Попытка получить данные с серверов bioRxiv/medRxiv ---
        servers_to_try = ['biorxiv', 'medrxiv']
        api_preprint_data_collection = None
        actual_server_name = None

        for server_name_attempt in servers_to_try:
            temp_api_url = f"https://api.biorxiv.org/details/{server_name_attempt}/{clean_doi_for_query}/na/json"
            # headers = {'User-Agent': f'ScientificPapersApp/1.0 (mailto:{APP_EMAIL})'}
            headers = {'User-Agent': USER_AGENT_LIST[0]}
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                f'Attempting a request to {server_name_attempt.upper()}: {temp_api_url}',
                progress_percent=10,
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )

            try:
                response = requests.get(temp_api_url, headers=headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    if data and data.get('collection') and isinstance(data['collection'], list) and data['collection']:
                        # Проверяем DOI в ответе для большей точности
                        # (иногда API может вернуть ближайшее совпадение, если точного нет)
                        # Хотя для /details/[server]/[DOI] это маловероятно, но проверка не помешает.
                        # Первый элемент коллекции обычно самый свежий или единственный.
                        first_result_in_collection = data['collection'][0]
                        doi_in_response = first_result_in_collection.get('doi','').lower()
                        if doi_in_response == clean_doi_for_query:
                            api_preprint_data_collection = data['collection'] # Сохраняем всю коллекцию, если понадобится (но используем первый)
                            # final_api_url = temp_api_url
                            actual_server_name = server_name_attempt
                            send_user_notification(
                                user_id,
                                task_id,
                                query_display_name,
                                'PROGRESS',
                                f'The data was successfully obtained from {actual_server_name.upper()}.',
                                progress_percent=20,
                                source_api=current_api_name,
                                originating_reference_link_id=originating_reference_link_id
                            )
                            break
                        else:
                            send_user_notification(
                                user_id,
                                task_id,
                                query_display_name,
                                'INFO',
                                f'The DOI in the {actual_server_name.upper()} ({doi_in_response}) response did not match the \
                                requested one ({clean_doi_for_query}).',
                                source_api=current_api_name,
                                originating_reference_link_id=originating_reference_link_id
                            )
                    else:
                        send_user_notification(
                            user_id,
                            task_id,
                            query_display_name,
                            'INFO',
                            f'{server_name_attempt.upper()} returned an empty collection for the DOI.',
                            source_api=current_api_name,
                            originating_reference_link_id=originating_reference_link_id
                        )
                elif response.status_code == 404:
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'INFO',
                        f'Preprint not found on {server_name_attempt.upper()}.',
                        source_api=current_api_name,
                        originating_reference_link_id=originating_reference_link_id
                    )
                else:
                    response.raise_for_status() # Вызовет HTTPError для других кодов ошибок
            except requests.exceptions.RequestException as exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'WARNING',
                    f'Network error/API {server_name_attempt.upper()}: {str(exc)}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                if server_name_attempt == servers_to_try[-1]: # Если это последняя попытка
                    raise self.retry(exc=exc) # Повторяем задачу целиком
                time.sleep(1) # Пауза перед попыткой следующего сервера
            except json.JSONDecodeError as json_exc:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'WARNING',
                    f'JSON decoding error from {server_name_attempt.upper()}: {str(json_exc)}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                if server_name_attempt == servers_to_try[-1]:
                    return {'status': 'error', 'message': f'{server_name_attempt.upper()} JSON Decode Error: {str(json_exc)}'}
                time.sleep(1)
            except Exception as e_inner:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'WARNING',
                    f'Unexpected error when requesting to {server_name_attempt.upper()}: {str(e_inner)}.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                if server_name_attempt == servers_to_try[-1]:
                    return {
                        'status': 'error',
                        'message': f'Unexpected error during {server_name_attempt.upper()} request: {str(e_inner)}'
                    }
                time.sleep(1)

        if not api_preprint_data_collection:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'NOT_FOUND',
                f'Preprint not found on any of the Rxiv servers ({", ".join(servers_to_try)}).',
                source_api=current_api_name,
                originating_reference_link_id=originating_reference_link_id
            )
            return {'status': 'not_found', 'message': 'Preprint not found on Rxiv servers.'}

        # Используем данные первого (и обычно единственного релевантного) элемента коллекции
        api_data = api_preprint_data_collection[0]
        send_user_notification(
            user_id,
            task_id, query_display_name,
            'PROGRESS',
            f'Data {current_api_name} ({actual_server_name}) received, processing...',
            progress_percent=40,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )

        # pdf_to_save = None
        # api_pdf_link = None
        api_server_name_from_data = api_data.get('server', actual_server_name)
        api_doi_from_rxiv = api_data.get('doi', clean_doi_for_query).lower() # Используем исходный DOI, если API его не вернул
        version_str = api_data.get('version', '1')
        article_data['rxiv_version'] = version_str

        article_owner = None
        if user_id:
            try:
                article_owner = User.objects.get(id=user_id)
            except User.DoesNotExist:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'FAILURE',
                    f'User ID {user_id} not found.',
                    source_api=current_api_name,
                    originating_reference_link_id=originating_reference_link_id
                )
                return {'status': 'error', 'message': f'User with ID {user_id} not found.'}

        # Извлечение данных из ответа API Rxiv
        article_data['title'] = api_data.get('title')
        article_data['abstract'] = api_data.get('abstract')
        api_date_str = api_data.get('date') # Дата постинга
        api_parsed_date = None
        if api_date_str:
            try:
                api_parsed_date = timezone.datetime.strptime(api_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        if api_parsed_date:
            article_data['publication_date'] = api_parsed_date

        # api_server_name_from_data = api_data.get('server', actual_server_name)
        api_category = api_data.get('category')
        api_journal_name_construct = f"{api_server_name_from_data.upper()} ({api_category or 'N/A'})" if api_server_name_from_data else None

        if api_journal_name_construct:
            article_data['journal_name'] = api_journal_name_construct

        api_parsed_authors = parse_rxiv_authors(api_data.get('authors'))
        article_data['current_api_name'] = current_api_name
        article_data['primary_source_api'] = current_api_name
        article_data['doi'] = api_doi_from_rxiv # Убедимся, что DOI сохранен

        # if api_parsed_authors:
        #     if can_fully_overwrite or not article.authors.exists():
        #         article.articleauthororder_set.all().delete()
        #         for order, author_obj in enumerate(api_parsed_authors):
        #             ArticleAuthorOrder.objects.create(article=article, author=author_obj, order=order)

        # --- Обработка полного текста JATS XML ---
        full_text_xml_rxvi = None
        api_jats_xml_url = api_data.get('jatsxml')
        if api_jats_xml_url:
            send_user_notification(
                user_id,
                task_id,
                query_display_name,
                'PROGRESS',
                'Found JATS XML Rxiv. Loading...',
                progress_percent=60,
                source_api=current_api_name
            )
            try:
                time.sleep(2)
                # # headers = {'User-Agent': f'{USER_AGENT_LIST[0]} (mailto:{APP_EMAIL})'}
                # headers = {'User-Agent': f'{USER_AGENT_LIST[0]}'}
                # jats_response = requests.get(api_jats_xml_url, timeout=60, headers=headers)
                # print(f'*** DEBUG (fetch_data_from_rxiv_task) RXIV headers: {headers} \napi_jats_xml_url: {api_jats_xml_url} \njats_response: {jats_response}')
                # jats_response.raise_for_status()
                # full_text_xml_rxvi = jats_response.text
                response_biorxiv = get_xml_from_biorxiv(api_jats_xml_url)
                if response_biorxiv['status'] == 'success':
                    full_text_xml_rxvi = response_biorxiv['data']
                elif response_biorxiv['status'] == 'error':
                    print(f'*** DEBUG (get_xml_from_biorxiv) ERROR: {response_biorxiv['message']}')
                    send_user_notification(
                        user_id,
                        task_id,
                        query_display_name,
                        'WARNING',
                        f'Failed to retrieve JATS XML from Rxiv: {response_biorxiv['message']}',
                        source_api=current_api_name
                    )
            except Exception as e_jats:
                send_user_notification(
                    user_id,
                    task_id,
                    query_display_name,
                    'WARNING',
                    f'Error when processing JATS XML from Rxiv: {str(e_jats)}',
                    source_api=current_api_name
                )

        if full_text_xml_rxvi:
            article_data['article_contents'] = {'full_text_xml_rxvi': full_text_xml_rxvi} # json.dumps(full_text_xml_content)

        print('*** DEBUG (fetch_data_from_rxiv_task) RXIV:')
        for k, v in article_data.items():
            print(f'key: {k}, val: {v}')

        final_message = f'Preprint {current_api_name} ({api_server_name_from_data}) "{article_data['title'][:30]}..." {"received"}.'
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'SUCCESS',
            final_message,
            progress_percent=100,
            # article_id=article.id,
            # created=created,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )
        return {
            'status': 'success',
            'message': final_message,
            'identifier': query_display_name,
            'article_data': article_data
            # 'article_data': {}
        }

    except Exception as e:
        error_message_for_user = f'Internal error {current_api_name}: {type(e).__name__} - {str(e)}'
        self.update_state(
            state='FAILURE',
            meta={
                'identifier': query_display_name,
                'error': error_message_for_user,
                'traceback': self.request.exc_info if hasattr(self.request, 'exc_info') else str(e)
            }
        )
        send_user_notification(
            user_id,
            task_id,
            query_display_name,
            'FAILURE',
            error_message_for_user,
            source_api=current_api_name,
            originating_reference_link_id=originating_reference_link_id
        )
        return {'status': 'error', 'message': f'Internal error: {str(e)}', 'identifier': query_display_name}


# Запрос к CrossRef для поиска DOI для цитируемой ссылки
@shared_task(bind=True, max_retries=2, default_retry_delay=180)
def find_doi_for_reference_task(self, reference_link_id: int, user_id: int):
    task_id = self.request.id
    display_identifier = f"RefLinkID:{reference_link_id}"

    send_user_notification(
        user_id,
        task_id,
        display_identifier,
        'PENDING',
        'Starting a DOI search for a reference...',
        source_api=FIND_DOI_TASK_SOURCE_NAME,
        originating_reference_link_id=reference_link_id
    )

    try:
        ref_link = ReferenceLink.objects.select_related('source_article__user').get(id=reference_link_id)
        ref_link.status = ReferenceLink.StatusChoices.DOI_LOOKUP_IN_PROGRESS
        ref_link.save(update_fields=['status', 'updated_at'])
    except ReferenceLink.DoesNotExist:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'Link not found in the database.',
            source_api=FIND_DOI_TASK_SOURCE_NAME,
            originating_reference_link_id=reference_link_id
        )
        return {'status': 'error', 'message': 'ReferenceLink not found.'}

    if ref_link.source_article.user_id != user_id:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'No rights to perform this action.',
            source_api=FIND_DOI_TASK_SOURCE_NAME,
            originating_reference_link_id=reference_link_id
        )
        return {'status': 'error', 'message': 'Permission denied.'}

    if ref_link.target_article_doi:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'INFO',
            f'The DOI ({ref_link.target_article_doi}) is already specified for this link.',
            source_api=FIND_DOI_TASK_SOURCE_NAME,
            originating_reference_link_id=reference_link_id
        )
        return {'status': 'info', 'message': 'DOI already exists for this reference link.'}

    # Формируем поисковый запрос для CrossRef
    # Используем данные из manual_data_json (если есть название, авторы, год) или raw_reference_text
    search_query_parts = []
    if ref_link.manual_data_json:
        if ref_link.manual_data_json.get('title'):
            search_query_parts.append(str(ref_link.manual_data_json['title']))
        # Можно добавить авторов, если они есть в structured виде, например, первый автор
        # if ref_link.manual_data_json.get('authors') and isinstance(ref_link.manual_data_json['authors'], list):
        #    search_query_parts.append(str(ref_link.manual_data_json['authors'][0].get('name')))
        if ref_link.manual_data_json.get('year'):
            search_query_parts.append(str(ref_link.manual_data_json['year']))

    if not search_query_parts and ref_link.raw_reference_text: # Если нет структурированных данных, берем сырой текст
        search_query_parts.append(ref_link.raw_reference_text[:300]) # Ограничим длину запроса

    if not search_query_parts:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'Insufficient data for DOI search.',
            source_api=FIND_DOI_TASK_SOURCE_NAME,
            originating_reference_link_id=reference_link_id
        )
        ref_link.status = ReferenceLink.StatusChoices.ERROR_DOI_LOOKUP # Или новый статус "недостаточно данных"
        ref_link.save(update_fields=['status', 'updated_at'])
        return {'status': 'error', 'message': 'Not enough data to search for DOI.'}

    bibliographic_query = " ".join(search_query_parts)

    params = {
        'query.bibliographic': bibliographic_query,
        'rows': 1, # Нам нужен только самый релевантный результат
        'mailto': APP_EMAIL
    }
    api_url = "https://api.crossref.org/works"
    # headers = {'User-Agent': f'ScientificPapersApp/1.0 (mailto:{APP_EMAIL})'}
    headers = {'User-Agent': USER_AGENT_LIST[0]}

    try:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'PROGRESS',
            f'CrossRef query for DOI search: "{bibliographic_query[:50]}..."',
            progress_percent=30,
            source_api=FIND_DOI_TASK_SOURCE_NAME,
            originating_reference_link_id=reference_link_id
        )
        response = requests.get(api_url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'RETRYING',
            f'CrossRef network/API error when searching for DOI: {str(exc)}. Repeat...',
            source_api=FIND_DOI_TASK_SOURCE_NAME,
            originating_reference_link_id=reference_link_id
        )
        raise self.retry(exc=exc)

    if data and data.get('message') and data['message'].get('items'):
        found_item = data['message']['items'][0]
        found_doi = found_item.get('DOI')
        score = found_item.get('score', 0) # CrossRef возвращает score релевантности

        if found_doi:
            # Можно добавить проверку score, чтобы отсеять совсем нерелевантные результаты
            # Например, if score > некоторого порога (например 60-70)
            ref_link.target_article_doi = found_doi.lower()
            ref_link.status = ReferenceLink.StatusChoices.DOI_PROVIDED_NEEDS_LOOKUP
            # Можно сохранить и другую информацию, например, название найденной статьи, в manual_data_json для сверки
            if 'title' in found_item and isinstance(found_item['title'], list) and found_item['title']:
                ref_link.manual_data_json = ref_link.manual_data_json or {}
                ref_link.manual_data_json['found_title_by_doi_search'] = found_item['title'][0]
                ref_link.manual_data_json['found_doi_score'] = score

            ref_link.save(update_fields=['target_article_doi', 'status', 'manual_data_json', 'updated_at'])
            send_user_notification(
                user_id,
                task_id,
                display_identifier,
                'SUCCESS',
                f'Found DOI: {found_doi} (score: {score}) for reference.',
                progress_percent=100,
                source_api=FIND_DOI_TASK_SOURCE_NAME,
                originating_reference_link_id=reference_link_id
            )
            return {'status': 'success', 'message': f'DOI found: {found_doi}', 'found_doi': found_doi}
        else:
            ref_link.status = ReferenceLink.StatusChoices.ERROR_DOI_LOOKUP # Или новый статус "DOI не найден"
            ref_link.save(update_fields=['status', 'updated_at'])
            send_user_notification(
                user_id,
                task_id,
                display_identifier,
                'FAILURE',
                'DOI не найден в ответе CrossRef.',
                progress_percent=100,
                source_api=FIND_DOI_TASK_SOURCE_NAME,
                originating_reference_link_id=reference_link_id
            )
            return {'status': 'not_found', 'message': 'DOI not found in CrossRef response.'}
    else:
        ref_link.status = ReferenceLink.StatusChoices.ERROR_DOI_LOOKUP # Или новый статус "DOI не найден"
        ref_link.save(update_fields=['status', 'updated_at'])
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'DOI not found (empty response from CrossRef).',
            progress_percent=100,
            source_api=FIND_DOI_TASK_SOURCE_NAME,
            originating_reference_link_id=reference_link_id
        )
        return {'status': 'not_found', 'message': 'DOI not found (empty response from CrossRef).'}


@shared_task(bind=True, max_retries=2, default_retry_delay=180)
def process_full_text_and_create_segments_task(self, article_id: int, user_id: int):
    """
    Обрабатывает полный текст статьи в JATS XML для автоматического создания
    анализируемых сегментов (AnalyzedSegment) и их связи с библиографическими ссылками.
    """
    task_id = self.request.id
    current_api_name = "SegmentLinker"
    display_identifier = f"ArticleID:{article_id}"

    send_user_notification(
        user_id,
        task_id,
        display_identifier,
        'PENDING',
        'Starting automatic linking of text and links...',
        source_api=current_api_name
    )

    try:
        article = Article.objects.get(id=article_id, user_id=user_id)
        # Находим JATS XML для этой статьи. Предпочитаем PMC, затем Rxiv, затем любой другой JATS.
        xml_content_entry = ArticleContent.objects.filter(
            article=article,
            format_type__in=['full_text_xml_pmc', 'full_text_xml_rxvi', 'full_text_xml_europepmc']
            # format_type__in=['pmc_fulltext_xml', 'rxiv_jats_xml_fulltext', 'xml_jats_fulltext']
        ).order_by('-retrieved_at').first()

        if not xml_content_entry or not xml_content_entry.content:
            send_user_notification(
                user_id,
                task_id,
                display_identifier,
                'INFO',
                'Full JATS XML text for creating segments was not found.',
                source_api=current_api_name
            )
            return {'status': 'info', 'message': 'No JATS XML found for segmentation.'}

        xml_string = xml_content_entry.content

        # --- Парсинг списка литературы и создание/обновление ReferenceLink ---
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'PROGRESS',
            'Parsing a reference list from XML...',
            progress_percent=20,
            source_api=current_api_name
        )

        print('*** DEBUG (process_full_text_and_create_segments_task) segments > parse_references_from_jats')
        parsed_references = parse_references_from_jats(xml_string)
        if not parsed_references:
            send_user_notification(
                user_id,
                task_id,
                display_identifier,
                'WARNING',
                'Failed to extract links from JATS XML. Linking will be incomplete.',
                source_api=current_api_name
            )

        for ref_data in parsed_references:
            if not ref_data.get('jats_ref_id'):
                continue # Пропускаем ссылки без внутреннего ID, их не связать

            defaults = {
                'raw_reference_text': ref_data.get('raw_text'),
                'manual_data_json': {
                    'jats_ref_id': ref_data.get('jats_ref_id'),
                    'title': ref_data.get('title'),
                    'year': ref_data.get('year'),
                    'doi_from_source': ref_data.get('doi')
                }
            }
            if ref_data.get('doi'):
                defaults['target_article_doi'] = ref_data['doi']
                defaults['status'] = ReferenceLink.StatusChoices.DOI_PROVIDED_NEEDS_LOOKUP
            else:
                defaults['status'] = ReferenceLink.StatusChoices.PENDING_DOI_INPUT

            # Ищем/создаем ссылку по ее JATS ID, который уникален в рамках статьи
            ReferenceLink.objects.update_or_create(
                source_article=article,
                manual_data_json__jats_ref_id=ref_data.get('jats_ref_id'),
                defaults=defaults
            )

        # --- Создание карты ссылок для быстрого сопоставления ---
        ref_map = {
            ref.manual_data_json['jats_ref_id']: ref
            for ref in ReferenceLink.objects.filter(source_article=article)
            if ref.manual_data_json and 'jats_ref_id' in ref.manual_data_json
        }

        # --- Парсинг тела статьи, создание и связывание сегментов ---
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'PROGRESS',
            'Analysing text and creating segments...',
            progress_percent=50,
            source_api=current_api_name
        )

        # Удаляем существующие автоматически созданные сегменты, чтобы избежать дублей при перезапуске
        # Мы договорились, что у системных сегментов user=None
        AnalyzedSegment.objects.filter(article=article, user__isnull=True).delete()

        xml_string_no_ns = re.sub(r'\sxmlns="[^"]+"', '', xml_string, count=1)
        root = ET.fromstring(xml_string_no_ns)
        body_node = root.find('.//body')

        segments_created = 0
        if body_node is not None:
            # Итерация по секциям для определения section_key
            for sec_node in body_node.findall('.//sec'): # Ищем все секции рекурсивно
                sec_title_el = sec_node.find('./title')
                section_key = "".join(sec_title_el.itertext()).strip() if sec_title_el is not None else "Unnamed Section"

                for p_node in sec_node.findall('./p'): # Итерация по абзацам внутри секции
                    segment_text = "".join(p_node.itertext()).strip()
                    if not segment_text or len(segment_text) < 50: # Пропускаем очень короткие параграфы
                        continue

                    inline_xrefs = p_node.findall('.//xref[@ref-type="bibr"]')
                    cited_ref_links_for_segment = set() # Используем set для автоматического исключения дублей
                    inline_markers_in_segment = []

                    if inline_xrefs:
                        for xref in inline_xrefs:
                            # Атрибут `rid` может содержать несколько ID, разделенных пробелами (например, "CR1 CR5")
                            rids = xref.get('rid', '').split()
                            for rid in rids:
                                if rid in ref_map:
                                    cited_ref_links_for_segment.add(ref_map[rid])
                            if xref.text:
                                inline_markers_in_segment.append(xref.text.strip())

                    # Создаем сегмент. Он создается всегда, даже если в нем нет ссылок, так как он является логической частью текста.
                    segment = AnalyzedSegment.objects.create(
                        article=article,
                        section_key=section_key,
                        segment_text=segment_text,
                        inline_citation_markers=list(set(inline_markers_in_segment)) or None, # Сохраняем уникальные маркеры
                        user=None # Системное создание
                    )

                    # Устанавливаем M2M связь
                    if cited_ref_links_for_segment:
                        segment.cited_references.set(list(cited_ref_links_for_segment))

                    segments_created += 1

        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'SUCCESS',
            f'Automatic linking is complete. {segments_created} segments created.',
            progress_percent=100,
            source_api=current_api_name
        )
        return {'status': 'success', 'message': f'Created {segments_created} segments.'}

    except Article.DoesNotExist:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'Article not found for analysis (process_full_text_and_create_segments_task).',
            source_api=current_api_name
        )
        return {'status': 'error', 'message': 'Article not found (process_full_text_and_create_segments_task).'}
    except Exception as e:
        error_message_for_user = f'Error during automatic binding: {type(e).__name__} - {str(e)}'
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            error_message_for_user,
            source_api=current_api_name
        )
        self.update_state(
            state='FAILURE',
            meta={
                'identifier': display_identifier,
                'error': error_message_for_user,
                'traceback': self.request.exc_info if hasattr(self.request, 'exc_info') else str(e)
            }
        )
        return {'status': 'error', 'message': error_message_for_user}


@shared_task(bind=True, max_retries=2, default_retry_delay=300) # LLM запросы могут быть долгими
def analyze_segment_with_llm_task(self, analyzed_segment_id: int, user_id: int):
    task_id = self.request.id
    current_api_name = "LLM_Analysis"
    display_identifier = f"AnalyzedSegmentID:{analyzed_segment_id}"

    send_user_notification(
        user_id,
        task_id,
        display_identifier,
        'PENDING',
        'Beginning LLM segment analysis...',
        progress_percent=0,
        source_api=current_api_name
    )

    try:
        segment = AnalyzedSegment.objects.select_related('article__user').prefetch_related('cited_references__resolved_article').get(id=analyzed_segment_id)
    except AnalyzedSegment.DoesNotExist:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'The analysed segment is not found.',
            source_api=current_api_name
        )
        return {'status': 'error', 'message': 'AnalyzedSegment not found.'}

    if segment.article.user_id != user_id:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'No rights to analyse this segment.',
            source_api=current_api_name
        )
        return {'status': 'error', 'message': 'Permission denied for this segment.'}

    if not segment.segment_text:
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            'Segment text is empty, cannot be analysed.',
            source_api=current_api_name
        )
        return {'status': 'error', 'message': 'Segment text is empty.'}

    # --- Подготовка данных для промпта ---
    cited_references = {}
    cited_references_info = []
    for i, ref_link in enumerate(segment.cited_references.all()):
        ref_id = ''
        ref_title = ''
        # ref_abstract = "N/A"
        ref_text = ''
        if 'jats_ref_id' in ref_link.manual_data_json and ref_link.manual_data_json['jats_ref_id']:
            ref_id = ref_link.manual_data_json["jats_ref_id"]
        if ref_link.resolved_article:
            ref_title = ref_link.resolved_article.title
            if ref_link.resolved_article.cleaned_text_for_llm:
                ref_text = ref_link.resolved_article.cleaned_text_for_llm
            elif ref_link.resolved_article.pdf_text:
                ref_text = ref_link.resolved_article.pdf_text
            elif ref_link.resolved_article.abstract:
                ref_text = ref_link.resolved_article.abstract
            cited_references[ref_id] = {'title': ref_title, 'text': ref_text}

    if cited_references:
        sorted_cited_references = dict(sorted(cited_references.items()))
        for k, v in sorted_cited_references.items():
            if cited_references_info:
                cited_references_info.append('-' * 60)
            cited_references_info.append(f"\nSource [{k}]:\nTitle: {v['title']}\nText: {v['text']}\n")

    cited_references_text = "\n".join(cited_references_info) if cited_references_info else "Information on cited sources is not provided."

    # Формирование промпта
    prompt = f"""You are acting as a scientific assistant. You are given a text segment from a scientific article and information about sources cited in it (or relevant to it).
    Your task:
    1. Carefully read the text segment.
    2. Study the provided information about cited sources.
    3. Assess how well the claims made in the text segment are supported by or relate to the information from these sources.
    4. Form a brief textual analysis (2-5 sentences) describing your assessment.
    5. Give a numerical confidence score for how well the segment's claims are supported by the sources on a scale from 1 (no support/contradiction) to 5 (complete and clear support).

    Text segment for analysis:
    ------- SEGMENT -------
    {segment.segment_text}
    ------- END OF SEGMENT -------

    Information about cited/relevant sources:
    ------- SOURCE INFORMATION -------
    {cited_references_text}
    ------- END OF SOURCE INFORMATION -------

    Provide your answer strictly in JSON format with the following keys:
    "analysis_notes": "Your textual analysis here.",
    "veracity_score": number from 1 to 5 (e.g., 3 or 4.5)."""

    print(f'segment.id: {segment.id}, PROMPT len: {len(prompt)}, PROMPT words count: {len(prompt.split())} \n')
    print(prompt)

    send_user_notification(
        user_id,
        task_id,
        display_identifier,
        'PROGRESS',
        'Sending a request to the LLM...',
        progress_percent=30,
        source_api=current_api_name
    )

    llm_response_content = None
    llm_model_used = None

    try:
        if getattr(settings, 'LLM_PROVIDER_FOR_ANALYSIS', None) == "OpenAI" and settings.OPENAI_API_KEY:
            # client = OpenAI(api_key=settings.OPENAI_API_KEY)
            # llm_model_used = "gpt-3.5-turbo" # или gpt-4o, gpt-4-turbo
            # llm_model_used = getattr(settings, 'OPENAI_DEFAULT_MODEL', 'gpt-4o-mini')

            client = OpenAI(
                base_url="http://80.209.242.40:8000/v1",
                api_key="dummy-key"
            )
            llm_model_used = "llama-3.3-70b-instruct"

            chat_completion = client.chat.completions.create(
                model=llm_model_used,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an attentive research assistant who analyses the text and its sources and always responds in JSON format."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                # temperature=0.3, # Более детерминированный ответ
                response_format={"type": "json_object"} # Если используете GPT-4 Turbo или новее с поддержкой JSON mode
            )

            raw_llm_output = chat_completion.choices[0].message.content
            print(f"***** OpenAI raw_llm_output: {raw_llm_output}") # Для отладки
            try:
                llm_response_content = json.loads(raw_llm_output)
            except json.JSONDecodeError:
                json_match = re.search(r'\{.*\}', raw_llm_output, re.DOTALL)
                if json_match:
                    try:
                        llm_response_content = json.loads(json_match.group(0))
                    except json.JSONDecodeError:
                        llm_response_content = {
                            "analysis_notes": f"LLM returned the text but failed to extract the JSON: {raw_llm_output}",
                            "veracity_score": None
                        }
                else:
                    llm_response_content = {
                        "analysis_notes": f"LLM returned a non-JSON response: {raw_llm_output}",
                        "veracity_score": None
                    }

        elif getattr(settings, 'LLM_PROVIDER_FOR_ANALYSIS', None) == "Grok":
            raw_llm_output = send_prompt_to_grok(prompt) # Запрос к Grok
            print(f"***** Grok raw_llm_output: {raw_llm_output}") # Для отладки

            try:
                if raw_llm_output:
                    llm_response_content = json.loads(raw_llm_output)
                    llm_model_used = 'grok'
            except json.JSONDecodeError:
                json_match = re.search(r'\{.*\}', raw_llm_output, re.DOTALL)
                if json_match:
                    try:
                        llm_response_content = json.loads(json_match.group(0))
                    except json.JSONDecodeError:
                        llm_response_content = {
                            "analysis_notes": f"LLM returned the text but failed to extract the JSON: {raw_llm_output}",
                            "veracity_score": None
                        }
                else:
                    llm_response_content = {
                        "analysis_notes": f"LLM returned a non-JSON response: {raw_llm_output}",
                        "veracity_score": None
                    }

        else:
            send_user_notification(
                user_id,
                task_id,
                display_identifier,
                'WARNING',
                'LLM is not configured. A blanking plug is used.',
                source_api=current_api_name
            )
            time.sleep(2)
            llm_response_content = {
                "analysis_notes": "Plug: LLM analysis not performed because LLM is not configured.",
                "veracity_score": None
            }
            llm_model_used = "stub_model"

        if llm_response_content and isinstance(llm_response_content, dict):
            segment.llm_analysis_notes = llm_response_content.get("analysis_notes", "Нет текстового анализа от LLM.")
            score = llm_response_content.get("veracity_score")
            try:
                segment.llm_veracity_score = float(score) if score is not None else None
            except (ValueError, TypeError):
                segment.llm_veracity_score = None
            segment.llm_model_name = llm_model_used
            segment.prompt_used = prompt
            segment.save(update_fields=['llm_analysis_notes', 'llm_veracity_score', 'llm_model_name', 'prompt_used', 'updated_at'])

            send_user_notification(
                user_id,
                task_id,
                display_identifier,
                'SUCCESS',
                'LLM analysis of the segment has been successfully completed.',
                progress_percent=100,
                source_api=current_api_name,
                analysis_data={
                    'segment_id': segment.id,
                    'notes': segment.llm_analysis_notes,
                    'score': segment.llm_veracity_score,
                    'model': llm_model_used
                }
            )
            return {'status': 'success', 'message': 'LLM analysis complete.', 'analyzed_segment_id': segment.id}
        else:
            send_user_notification(
                user_id,
                task_id,
                display_identifier,
                'FAILURE',
                'LLM returned an invalid or empty response.',
                source_api=current_api_name
            )
            # TODO: (сохранение ошибки в segment.llm_analysis_notes) ...
            return {'status': 'error', 'message': 'LLM returned invalid or empty response.'}

    except Exception as e:
        error_message_for_user = f'Ошибка во время LLM анализа: {type(e).__name__} - {str(e)}'
        # TODO: (сохранение ошибки в segment.llm_analysis_notes) ...
        send_user_notification(
            user_id,
            task_id,
            display_identifier,
            'FAILURE',
            error_message_for_user,
            source_api=current_api_name,
        )
        return {'status': 'error', 'message': f'LLM analysis failed: {str(e)}'}
