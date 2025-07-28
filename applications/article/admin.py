from adminsortable2.admin import SortableAdminBase, SortableAdminMixin, SortableInlineAdminMixin, SortableStackedInline
from django import forms
from django.contrib import admin
from django.utils.html import format_html, mark_safe

from .models import AnalyzedSegment, Article, ArticleAuthor, ArticleContent, Author, ReferenceLink


class ArticleAuthorInline(admin.TabularInline):
    model = ArticleAuthor
    extra = 0
    autocomplete_fields = ['author', 'article']


class ArticleContentInline(admin.StackedInline):
    """
    Позволяет просматривать и добавлять сырой контент прямо на странице статьи.
    """
    model = ArticleContent
    extra = 0
    readonly_fields = ('retrieved_at',) # Это поле заполняется автоматически
    classes = ['collapse']


class ReferenceLinkInlineForm(forms.ModelForm):
    class Meta:
        model = ReferenceLink
        fields = '__all__'
        widgets = {
            'raw_reference_text': forms.Textarea(attrs={'rows': 3, 'cols': 70}),
            'target_article_doi': forms.TextInput(attrs={'size': 50}),
            'manual_data_json': forms.Textarea(attrs={'rows': 3, 'cols': 70}),
        }


# class ReferenceLinkInline(admin.StackedInline):
class ReferenceLinkInline(SortableStackedInline):
    """
    Позволяет просматривать и добавлять библиографические ссылки, сделанные из текущей статьи.
    """
    model = ReferenceLink
    fk_name = 'source_article' # Явно указываем ForeignKey на Article
    form = ReferenceLinkInlineForm
    extra = 0
    classes = ['collapse']
    autocomplete_fields = ['resolved_article'] # Удобный поиск связанных статей
    readonly_fields = ('created_at', 'updated_at')
    # Поля для отображения в инлайне
    fields = ('raw_reference_text', 'target_article_doi', 'resolved_article', 'status', 'manual_data_json')

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj:
            count = obj.references_made.count()
            self.verbose_name_plural = f"Библиографические ссылки: {count}"
        else:
            self.verbose_name_plural = "Библиографические ссылки"
        return formset

    def reference_link_inline_count(self, obj):
        return obj.references_made.count()


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'pk', 'orcid', 'created')
    search_fields = ('full_name', 'last_name', 'orcid')
    readonly_fields = ('created', 'updated')
    inlines = [ArticleAuthorInline]
    # fieldsets = (
    #     (None, {
    #         'fields': ('full_name', 'first_name', 'middle_name', 'last_name')
    #     }),
    #     ('Идентификаторы', {
    #         'fields': ('orcid')
    #     }),
    #     ('Метаданные Публикации', {
    #         'fields': ('affiliation')
    #     }),
    #     ('Даты', {
    #         'fields': ('created', 'updated'),
    #         'classes': ('collapse',)
    #     }),
    # )


@admin.register(ArticleAuthor)
class ArticleAuthorAdmin(admin.ModelAdmin):
    list_display = ['pk', 'author', 'article']


class ArticleAdminForm(forms.ModelForm):
    class Meta:
        model = Article
        fields = '__all__'
        widgets = {
            'structured_content': forms.Textarea(attrs={'rows': 20, 'cols': 70}),
        }


@admin.register(Article)
class ArticleAdmin(SortableAdminBase, admin.ModelAdmin):
    list_display = (
        'title',
        'id',
        'doi',
        'pubmed_id',
        'pmc_id_label',
        'arxiv_id',
        'reference_link_inline_count',
        'is_structured_content',
        'is_pdf_file',
        # 'best_oa_pdf_url',
        'user',
        'primary_source_api_label',
        'publication_date',
        'is_manually_added_full_text',
        'updated_at'
    )
    list_filter = ('publication_date', 'primary_source_api', 'is_manually_added_full_text', 'user')
    search_fields = ('title', 'doi', 'pubmed_id', 'pmc_id', 'arxiv_id', 'abstract', 'authors__full_name')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ['user']
    inlines = [ArticleAuthorInline, ArticleContentInline, ReferenceLinkInline]
    form = ArticleAdminForm

    fieldsets = (
        (None, {
            'fields': ('user', 'title', 'abstract')
        }),
        ('Identifiers and Sources', {
            'fields': ('doi', 'pubmed_id', 'pmc_id', 'arxiv_id', 'primary_source_api')
        }),
        ('Structured content', {
            'fields': ('structured_content',)
        }),
        ('Data for LLM and Manual Input', {
            'fields': ('cleaned_text_for_llm', 'is_manually_added_full_text')
        }),
        ('PDF File', {
            'fields': ('pdf_url', 'pdf_file', 'pdf_text')
        }),
        ('Publication metadata', {
            'fields': ('publication_date', 'journal_name')
        }),
        ('Dates', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def reference_link_inline_count(self, obj):
        return obj.references_made.count()
    reference_link_inline_count.short_description = 'Ref'

    def is_pdf_file(self, obj):
        if obj.pdf_file:
            return format_html('<span style="color: green;">&#10003;</span>')
        return format_html('<span style="color: red;">&#10007;</span>')
    is_pdf_file.short_description = 'PDF'

    def is_structured_content(self, obj):
        if obj.structured_content:
            return format_html('<span style="color: green;">&#10003;</span>')
        return format_html('<span style="color: red;">&#10007;</span>')
    is_structured_content.short_description = 'XML'

    def pmc_id_label(self, obj):
        return obj.pmc_id
    pmc_id_label.short_description = 'PMC'

    def primary_source_api_label(self, obj):
        return obj.primary_source_api
    primary_source_api_label.short_description = 'Источник'


@admin.register(AnalyzedSegment)
class AnalyzedSegmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'article', 'section_key', 'created_at']
    search_fields = ['article']