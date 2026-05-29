from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("select/", views.select_org, name="select"),
    path("create/", views.create_org, name="create"),
    path("switch/<int:org_id>/", views.switch_org, name="switch"),
    path("members/", views.members, name="members"),
    path("members/<int:membership_id>/role/", views.change_member_role, name="member-role"),
    path("members/<int:membership_id>/toggle/", views.toggle_member, name="member-toggle"),
    path("members/<int:membership_id>/remove/", views.remove_member, name="member-remove"),
]
