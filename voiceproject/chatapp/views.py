from django.shortcuts import render

def index(request):
    return render(request, 'chatapp/index.html')

def voice_assistant_view(request):
    return render(request, 'chatapp/index.html')
