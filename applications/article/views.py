from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View
from rest_framework import permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AnalyzedSegment, Article, ArticleContent, Author, ReferenceLink
from .serializers import (
    AnalyzedSegmentSerializer,
    ArticleContentSerializer,
    ArticleSerializer,
    AuthorSerializer,
    ReferenceLinkSerializer,
)
from .tasks import analyze_segment_with_llm_task, find_doi_for_reference_task, process_article_pipeline_task


class IsOwnerOfSourceArticle(permissions.BasePermission):
    """
    Разрешение, которое позволяет изменять/удалять объект
    только если пользователь является владельцем исходной статьи.
    """
    def has_object_permission(self, request, view, obj):
        # Разрешения на чтение разрешены всем (например, GET, HEAD, OPTIONS)
        if request.method in permissions.SAFE_METHODS:
            return True
        # Разрешения на запись даются только владельцу исходной статьи
        return obj.source_article.user == request.user


class AuthorViewSet(viewsets.ModelViewSet):
    """
    API endpoint, который позволяет просматривать и редактировать авторов.
    """
    queryset = Author.objects.all()
    serializer_class = AuthorSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]


class ArticleViewSet(viewsets.ModelViewSet):
    """
    API endpoint, который позволяет просматривать и редактировать статьи.
    """
    serializer_class = ArticleSerializer
    permission_classes = [permissions.IsAuthenticated] # Или IsAuthenticatedOrReadOnly

    def get_queryset(self):
        user = self.request.user
        if user.is_authenticated:
            # Админы видят всё, обычные пользователи - только свои статьи
            # Это также неявно защитит от удаления чужих статей через стандартный `destroy`
            if user.is_staff:
                return Article.objects.prefetch_related('articleauthororder_set__author', 'contents', 'references_made').select_related('user').all()
            return Article.objects.filter(user=user).prefetch_related('articleauthororder_set__author', 'contents', 'references_made').select_related('user')
        return Article.objects.none()

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ArticleContentViewSet(viewsets.ModelViewSet):
    """
    API endpoint для контента статей.
    Обычно управляется через инлайны статьи, но может быть полезен для прямого доступа.
    """
    queryset = ArticleContent.objects.all()
    serializer_class = ArticleContentSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]


class ReferenceLinkViewSet(viewsets.ModelViewSet):
    """
    API endpoint для библиографических ссылок.
    """
    queryset = ReferenceLink.objects.select_related('source_article', 'resolved_article').all()
    serializer_class = ReferenceLinkSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOfSourceArticle] # IsAuthenticated нужен, чтобы request.user был доступен

    def get_queryset(self):
        user = self.request.user
        if user.is_authenticated:
            if user.is_staff: # Админы видят все
                return super().get_queryset()
            # Пользователи видят ссылки, относящиеся к их статьям
            return super().get_queryset().filter(source_article__user=user)
        return ReferenceLink.objects.none()


class StartArticleProcessingView(View):
    def get(self, request, *args, **kwargs):
        identifier_value = request.GET.get('identifier')
        identifier_type = request.GET.get('type', 'DOI').upper()

        if not identifier_value:
            return JsonResponse({'error': 'The "identifier" parameter is missing'}, status=400)

        if not request.user.is_authenticated:
             return JsonResponse({'error': 'User not authenticated'}, status=401)

        # Запускаем диспетчерскую задачу
        task = process_article_pipeline_task.delay(
            identifier_value=identifier_value,
            identifier_type=identifier_type,
            user_id=request.user.id
        )

        return JsonResponse({
            'message': f'The processing pipeline for {identifier_type}:{identifier_value} has started.',
            'pipeline_task_id': task.id # ID диспетчерской задачи
        })


class LoadReferencedArticleAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, format=None):
        reference_link = get_object_or_404(ReferenceLink, pk=pk)

        # Проверка, что пользователь является владельцем исходной статьи
        if reference_link.source_article.user != request.user:
            return Response(
                {"error": "You do not have the rights to perform this action."},
                status=status.HTTP_403_FORBIDDEN
            )

        if not reference_link.target_article_doi:
            return Response(
                {"error": "The DOI for the cited article is not listed in this reference."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if reference_link.resolved_article:
            return Response(
                {"info": "This link is already linked to the uploaded article.", "article_id": reference_link.resolved_article_id},
                status=status.HTTP_200_OK # Или 409 Conflict, если считать это ошибкой повторного запроса
            )

        # Запускаем конвейер обработки для DOI из ссылки, передавая ID самой ссылки
        pipeline_task = process_article_pipeline_task.delay(
            identifier_value=reference_link.target_article_doi,
            identifier_type='DOI',
            user_id=request.user.id,
            originating_reference_link_id=reference_link.id
        )

        # reference_link.status = ReferenceLink.StatusChoices.DOI_LOOKUP_IN_PROGRESS  Обновляем статус ссылки
        reference_link.status = ReferenceLink.StatusChoices.ARTICLE_FETCH_IN_PROGRESS
        reference_link.save(update_fields=['status', 'updated_at'])

        return Response(
            {"message": "Request to download the cited article has been sent.", "task_id": pipeline_task.id},
            status=status.HTTP_202_ACCEPTED
        )


class FindDoiForReferenceAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, format=None):
        reference_link = get_object_or_404(ReferenceLink, pk=pk)

        if reference_link.source_article.user != request.user:
            return Response(
                {"error": "You do not have the rights to perform this action."},
                status=status.HTTP_403_FORBIDDEN
            )

        if reference_link.target_article_doi:
            return Response(
                {"info": f"The DOI ({reference_link.target_article_doi}) is already specified for this link."},
                status=status.HTTP_400_BAD_REQUEST # Или 200 OK с info, если это не считать ошибкой
            )

        if not reference_link.raw_reference_text and not (reference_link.manual_data_json and reference_link.manual_data_json.get('title')):
             return Response(
                {"error": "Insufficient data (reference text or title) for DOI search."},
                status=status.HTTP_400_BAD_REQUEST
            )

        task = find_doi_for_reference_task.delay(
            reference_link_id=reference_link.id,
            user_id=request.user.id
        )

        # Опционально: обновить статус ссылки немедленно на "в поиске"
        reference_link.status = ReferenceLink.StatusChoices.DOI_LOOKUP_IN_PROGRESS # Если такой статус есть, или PENDING_DOI_INPUT

        return Response(
            {"message": "The task of finding a DOI for a reference has been queued up.", "task_id": task.id},
            status=status.HTTP_202_ACCEPTED
        )


class FindAllReferenceDoisAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, format=None): # pk - это ID статьи
        article = get_object_or_404(Article, pk=pk)

        if article.user != request.user:
            return Response(
                {"error": "You do not have the rights to perform this action."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Находим все ссылки для этой статьи, у которых нет DOI и которые не находятся уже в процессе поиска DOI
        # или не завершились ошибкой, которую не стоит повторять автоматически.
        # Добавим фильтрацию по статусам, которые можно перепроверять.
        eligible_statuses_for_doi_search = [
            ReferenceLink.StatusChoices.PENDING_DOI_INPUT,
            # Можно добавить статусы ошибок, которые мы хотим перепроверить, например:
            # ReferenceLink.StatusChoices.ERROR_DOI_LOOKUP,
            # ReferenceLink.StatusChoices.ARTICLE_NOT_FOUND (если поиск DOI был частью этого)
        ]

        references_to_process = ReferenceLink.objects.filter(
            source_article=article,
            target_article_doi__isnull=True, # Или target_article_doi='' если используете пустые строки
            status__in=eligible_statuses_for_doi_search
        )
        # Альтернативно, если не хотите __isnull=True:
        # from django.db.models import Q
        # references_to_process = ReferenceLink.objects.filter(
        #     Q(target_article_doi__isnull=True) | Q(target_article_doi=''),
        #     source_article=article,
        #     status__in=eligible_statuses_for_doi_search
        # )

        if not references_to_process.exists():
            return Response(
                {"info": "There are no references requiring a DOI search for this article."},
                status=status.HTTP_200_OK
            )

        tasks_queued_count = 0
        for ref_link in references_to_process:
            # Обновляем статус на "в поиске" перед постановкой задачи
            ref_link.status = ReferenceLink.StatusChoices.DOI_LOOKUP_IN_PROGRESS
            ref_link.save(update_fields=['status', 'updated_at'])

            find_doi_for_reference_task.delay(
                reference_link_id=ref_link.id,
                user_id=request.user.id
            )
            tasks_queued_count += 1

        return Response(
            {"message": f"Started {tasks_queued_count} of DOI search tasks for references."},
            status=status.HTTP_202_ACCEPTED
        )


class LoadAllLinkedReferencesAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, format=None): # pk здесь - это ID статьи
        article = get_object_or_404(Article, pk=pk)

        if article.user != request.user:
            return Response(
                {"error": "You do not have the rights to perform this action."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Статусы, при которых мы можем инициировать загрузку, если DOI есть
        eligible_statuses_for_loading = [
            ReferenceLink.StatusChoices.DOI_PROVIDED_NEEDS_LOOKUP,
            # Можно добавить другие, если DOI был найден, но загрузка по какой-то причине не началась или прервалась
            # ReferenceLink.StatusChoices.PENDING_DOI_INPUT, # Если DOI был добавлен вручную, но статус не обновился
            # ReferenceLink.StatusChoices.ERROR_ARTICLE_FETCH, # Если хотим повторить попытку для всех ошибочных
        ]

        # Используем Q-объекты для непустого DOI
        references_to_load = ReferenceLink.objects.filter(
            Q(target_article_doi__isnull=False) & ~Q(target_article_doi=''), # DOI есть и не пустой
            source_article=article,
            resolved_article__isnull=True, # Статья еще не связана
            status__in=eligible_statuses_for_loading
        )

        if not references_to_load.exists():
            return Response(
                {"info": "There are no citations with DOIs waiting to be downloaded for this article."},
                status=status.HTTP_200_OK
            )

        tasks_queued_count = 0
        for ref_link in references_to_load:
            if ref_link.target_article_doi: # Дополнительная проверка на всякий случай
                ref_link.status = ReferenceLink.StatusChoices.ARTICLE_FETCH_IN_PROGRESS
                ref_link.save(update_fields=['status', 'updated_at'])

                process_article_pipeline_task.delay(
                    identifier_value=ref_link.target_article_doi,
                    identifier_type='DOI',
                    user_id=request.user.id,
                    originating_reference_link_id=ref_link.id
                )
                tasks_queued_count += 1

        return Response(
            {"message": f"Started {tasks_queued_count} of tasks to download cited articles."},
            status=status.HTTP_202_ACCEPTED
        )


class ReprocessArticleAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, format=None): # pk здесь - это ID статьи
        article = get_object_or_404(Article, pk=pk)

        if article.user != request.user:
            return Response(
                {"error": "You do not have the rights to perform this action."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Определяем основной идентификатор для перезапуска. Приоритет DOI.
        identifier_value = None
        identifier_type = None

        if article.doi:
            identifier_value = article.doi
            identifier_type = 'DOI'
        elif article.pubmed_id: # Если нет DOI, но есть PMID
            identifier_value = article.pubmed_id
            identifier_type = 'PMID'
        elif article.arxiv_id: # Если нет ни DOI, ни PMID, но есть arXiv ID
            identifier_value = article.arxiv_id
            identifier_type = 'ARXIV'
        # TODO: можно добавить другие идентификаторы, если они существуют

        if not identifier_value or not identifier_type:
            return Response(
                {"error": "Failed to determine the primary identifier for article re-processing."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Запускаем конвейер обработки для статьи.
        # originating_reference_link_id здесь не передается (None),
        # что означает "корневую" обработку статьи, включая ее ссылки (process_references=True для CrossRef).
        pipeline_task = process_article_pipeline_task.delay(
            identifier_value=identifier_value,
            identifier_type=identifier_type,
            user_id=request.user.id
            # originating_reference_link_id по умолчанию None
        )

        # Можно обновить какой-нибудь статус у самой статьи, например "ожидает переобработки"
        # article.processing_status = "reprocessing_queued" # Если бы у вас было такое поле
        # article.save()

        return Response(
            {"message": f"Request to rework article '{article.title[:50]}...' has been submitted.", "task_id": pipeline_task.id},
            status=status.HTTP_202_ACCEPTED
        )


class AnalyzedSegmentViewSet(viewsets.ModelViewSet):
    """
    API endpoint для анализируемых сегментов текста статьи.
    """
    serializer_class = AnalyzedSegmentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Пользователь может видеть/редактировать только сегменты статей, которыми он владеет
        user = self.request.user
        if user.is_staff: # Админы видят все
            return AnalyzedSegment.objects.select_related('article', 'user').prefetch_related('cited_references').all()
        return AnalyzedSegment.objects.select_related('article', 'user').prefetch_related('cited_references').filter(article__user=user)

    def perform_create(self, serializer):
        # Автоматически устанавливаем пользователя и статью (если она передается через URL или проверяется)
        # Если article_id передается в теле запроса, сериализатор его уже должен обработать
        # благодаря 'source' в PrimaryKeyRelatedField.
        # Мы просто устанавливаем пользователя.
        article_id = self.request.data.get('article_id') # Или self.kwargs.get('article_pk') для вложенных URL
        if not article_id:
            raise serializer.ValidationError({"article_id": "This field is mandatory."})
        try:
            article = Article.objects.get(pk=article_id, user=self.request.user)
        except Article.DoesNotExist:
            raise serializer.ValidationError({"article_id": "The specified article was not found or does not belong to you."})

        serializer.save(user=self.request.user, article=article)

    def perform_update(self, serializer):
        # При обновлении, user и article не должны меняться через этот эндпоинт.
        # Сериализатор уже должен был обработать `validated_data`.
        # Убедимся, что пользователь не пытается изменить сегмент чужой статьи (get_object сделает это).
        serializer.save()
        # Если бы user мог меняться, то: serializer.save(user=self.request.user)


class RunLLMAnalysisForSegmentAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, format=None): # pk - это ID AnalyzedSegment
        segment = get_object_or_404(AnalyzedSegment, pk=pk)

        # Проверка, что пользователь является владельцем статьи, к которой относится сегмент
        if segment.article.user != request.user:
            return Response(
                {"error": "You do not have permissions to analyse this segment."},
                status=status.HTTP_403_FORBIDDEN
            )

        if not segment.segment_text:
             return Response({"error": "Text segment empty."}, status=status.HTTP_400_BAD_REQUEST)

        # Запускаем Celery задачу
        # Передаем segment.id и request.user.id
        # section_text и section_key уже есть в объекте segment, задача их загрузит
        llm_task = analyze_segment_with_llm_task.delay(
            analyzed_segment_id=segment.id,
            user_id=request.user.id
            # analysis_type можно передавать из запроса, если хотите разные типы анализа
        )

        # Можно обновить статус сегмента на "анализ запущен"
        segment.llm_analysis_notes = "LLM analysis is running..."
        segment.llm_veracity_score = None # Сбрасываем предыдущий результат, если есть
        segment.save(update_fields=['llm_analysis_notes', 'llm_veracity_score', 'updated_at'])

        return Response(
            {"message": f"LLM analysis for segment ID {segment.id} is queued up.", "task_id": llm_task.id},
            status=status.HTTP_202_ACCEPTED
        )