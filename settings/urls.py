from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/articles/', include('applications.article.urls')),

    # Стандартные URL для аутентификации Django
    path('accounts/login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(next_page=reverse_lazy('login')), name='logout'), # next_page можно изменить
    # Позже можно добавить:
    # path('accounts/password_change/', auth_views.PasswordChangeView.as_view(), name='password_change'),
    # path('accounts/password_change/done/', auth_views.PasswordChangeDoneView.as_view(), name='password_change_done'),
    # path('accounts/password_reset/', auth_views.PasswordResetView.as_view(), name='password_reset'),
    # path('accounts/password_reset/done/', auth_views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    # path('accounts/reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    # path('accounts/reset/done/', auth_views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),
    # path('accounts/signup/', views.signup_view, name='signup'),

    path('', include('applications.article.urls_site')),
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # Также рекомендуется добавить обслуживание статических файлов, для сбора через collectstatic,
    # хотя runserver обычно их подхватывает из APP_DIRS.
    # urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT) # Это если STATIC_ROOT определен
