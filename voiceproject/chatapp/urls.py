from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('start/', views.start_assistant, name='start_assistant'),
    path('stop/', views.stop_assistant, name='stop_assistant'),
]
