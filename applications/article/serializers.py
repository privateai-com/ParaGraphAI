from rest_framework import serializers

from .models import AnalyzedSegment, Article, ArticleAuthor, ArticleContent, Author, ReferenceLink, User


class UserSerializer(serializers.ModelSerializer):
    """Сериализатор для модели User (только для чтения)."""
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name']


class AuthorSerializer(serializers.ModelSerializer):
    """Сеrialsizer для модели Author."""
    class Meta:
        model = Author
        fields = ['id', 'full_name']


class ArticleContentSerializer(serializers.ModelSerializer):
    """Сериализатор для модели ArticleContent."""
    class Meta:
        model = ArticleContent
        fields = ['id', 'article', 'source_api_name', 'format_type', 'content', 'retrieved_at']
        read_only_fields = ('retrieved_at',)


class ReferenceLinkSerializer(serializers.ModelSerializer):
    source_article_title = serializers.StringRelatedField(source='source_article.title', read_only=True)
    resolved_article_title = serializers.StringRelatedField(source='resolved_article.title', read_only=True)
    source_article = serializers.PrimaryKeyRelatedField(queryset=Article.objects.all())

    class Meta:
        model = ReferenceLink
        fields = [
            'id', 'source_article', 'source_article_title', 'raw_reference_text',
            'target_article_doi', 'resolved_article', 'resolved_article_title',
            'manual_data_json', 'status', 'log_messages', 'created_at', 'updated_at'
        ]
        read_only_fields = (
            'created_at', 'updated_at',
            'source_article_title', 'resolved_article_title',
            'resolved_article', # resolved_article по-прежнему обновляется только системой
            'log_messages'
        )

    def update(self, instance, validated_data):
        # Проверяем, изменился ли target_article_doi
        old_doi = instance.target_article_doi
        new_doi = validated_data.get('target_article_doi', old_doi)

        # Приводим DOI к нижнему регистру или None, если пустая строка
        if new_doi is not None:
            new_doi = new_doi.strip().lower()
            if not new_doi: # Если после strip() осталась пустая строка
                new_doi = None

        validated_data['target_article_doi'] = new_doi # Обновляем в validated_data для сохранения

        if old_doi != new_doi: # Если DOI изменился (или был добавлен/удален)
            instance.resolved_article = None # Сбрасываем связь со статьей
            if new_doi: # Если новый DOI есть
                instance.status = ReferenceLink.StatusChoices.DOI_PROVIDED_NEEDS_LOOKUP
            else: # Если DOI был удален (стал None)
                instance.status = ReferenceLink.StatusChoices.PENDING_DOI_INPUT

        # Обновляем остальные поля стандартным образом
        return super().update(instance, validated_data)


class ArticleAuthorSerializer(serializers.ModelSerializer):
    """Сериализатор для отображения авторов с порядком в статье."""
    # author = AuthorSerializer(read_only=True) # Если хотим полную инфу об авторе
    author_id = serializers.PrimaryKeyRelatedField(queryset=Author.objects.all(), source='author')
    author_name = serializers.StringRelatedField(source='author.full_name', read_only=True)

    class Meta:
        model = ArticleAuthor
        fields = ['author_id', 'author_name']


class ArticleSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), source='user', write_only=True, required=False)
    article_authors = ArticleAuthorSerializer(source='articleauthor_set', many=True, required=False)
    contents = ArticleContentSerializer(many=True, read_only=True)
    references_made = ReferenceLinkSerializer(many=True, read_only=True)
    # structured_content по умолчанию будет доступен для записи, т.к. это JSONField
    # cleaned_text_for_llm будет только для чтения, т.к. генерируется на бэкенде

    class Meta:
        model = Article
        fields = [
            'id', 'user', 'user_id', 'title', 'abstract', 'doi', 'pubmed_id', 'arxiv_id',
            'cleaned_text_for_llm', 'is_manually_added_full_text',
            'primary_source_api', 'publication_date', 'journal_name',
            'oa_status', 'best_oa_url', 'best_oa_pdf_url', 'oa_license', # Поля Unpaywall
            'is_user_initiated',
            'structured_content',
            'article_authors',
            'contents', 'references_made',
            'created_at', 'updated_at'
        ]
        read_only_fields = ('created_at', 'updated_at', 'user', 'cleaned_text_for_llm')

    def update(self, instance, validated_data):
        # Стандартное обновление полей, включая structured_content, если оно есть в validated_data
        # authors_data нужно обработать до super().update, если он не обрабатывает вложенные M2M по умолчанию
        authors_data = validated_data.pop('articleauthor_set', None)

        # Обновляем поля, которые не являются M2M или специальными
        # structured_content будет обновлен через super().update()
        instance = super().update(instance, validated_data)

        # Обработка авторов (если были переданы)
        if authors_data is not None:
            instance.articleauthor_set.all().delete()
            for author_data in authors_data:
                # Убедимся, что author_data['author'] - это объект Author
                author_instance = author_data.get('author')
                if isinstance(author_instance, Author):
                    ArticleAuthor.objects.create(article=instance, author=author_instance)
                    #  ArticleAuthor.objects.create(article=instance, author=author_instance, order=author_data.get('order',0))
                elif isinstance(author_instance, int): # Если передан ID
                    try:
                        author_obj = Author.objects.get(pk=author_instance)
                        ArticleAuthor.objects.create(article=instance, author=author_obj)
                        # ArticleAuthor.objects.create(article=instance, author=author_obj, order=author_data.get('order',0))
                    except Author.DoesNotExist:
                        pass # Логировать ошибку или пропустить

        return instance


class AnalyzedSegmentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True) # Отображаем пользователя, но устанавливаем его в perform_create
    # Для cited_references мы хотим принимать список ID при создании/обновлении
    cited_references = serializers.PrimaryKeyRelatedField(
        queryset=ReferenceLink.objects.all(),
        many=True,
        required=False # Сегмент может не иметь ссылок
    )
    # article также должен быть PrimaryKeyRelatedField при создании/обновлении, если не вложенный URL
    article_id = serializers.PrimaryKeyRelatedField(
        queryset=Article.objects.all(),
        source='article', # Указываем, что это поле 'article' в модели
        write_only=True # Только для записи, для чтения будет вложенная информация или ничего
    )

    class Meta:
        model = AnalyzedSegment
        fields = [
            'id', 'article', 'article_id', 'section_key', 'segment_text',
            'cited_references', 'inline_citation_markers',
            'llm_analysis_notes', 'llm_veracity_score',
            'user', 'created_at', 'updated_at'
        ]
        read_only_fields = ('user', 'created_at', 'updated_at')
        # Делаем article read_only, так как он будет устанавливаться в perform_create или через вложенный URL
        # или если мы принимаем article_id, то 'article' можно сделать read_only=True
        # и использовать article_id для записи.
        extra_kwargs = {
            'article': {'read_only': True} # Будем устанавливать в perform_create
        }

    def create(self, validated_data):
        # user устанавливается во ViewSet.perform_create
        # article также должен быть установлен во ViewSet.perform_create, если используется вложенный роутинг,
        # или если article_id передается в validated_data, как мы сделали с source='article'.
        # Если 'article' (объект) не был добавлен в validated_data сериализатором (из-за article_id),
        # то его нужно будет добавить перед super().create или в самом super().create.
        # В нашем случае ArticleSerializer имеет article_id для записи, так что это должно сработать.
        # Но для AnalyzedSegmentViewSet мы будем передавать article_id в URL или в теле,
        # и устанавливать article в perform_create во ViewSet.

        # Извлекаем cited_references, чтобы правильно их обработать после создания объекта
        cited_references_data = validated_data.pop('cited_references', [])
        segment = AnalyzedSegment.objects.create(**validated_data)
        if cited_references_data:
            segment.cited_references.set(cited_references_data)
        return segment

    def update(self, instance, validated_data):
        cited_references_data = validated_data.pop('cited_references', None)
        instance = super().update(instance, validated_data)
        if cited_references_data is not None: # Если передали пустой список - очистим, если передали данные - установим
            instance.cited_references.set(cited_references_data)
        return instance
