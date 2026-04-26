The web service is named web. On your DigitalOcean droplet, SSH in and run:


# SSH into your droplet first
ssh root@YOUR_DROPLET_IP

# Then run this (from the directory with docker-compose.prod.yml)
cd /opt/dafeapp   # or wherever you deployed it

docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
It will prompt you interactively:


Email: your@email.com
Password: ••••••••
Password (again): ••••••••
Superuser created successfully.
If you also need to mark them as is_platform_admin, do it right after:


docker compose -f docker-compose.prod.yml exec web python manage.py shell -c "
from users.models import User
u = User.objects.get(email='your@email.com')
u.is_platform_admin = True
u.save()
print('Done')
"
Then visit https://yourdomain.com/admin/ to log in.

root@Odoo18-Cloudpepper-Prod:/opt/dafeapp# docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
WARN[0000] /opt/dafeapp/docker-compose.prod.yml: the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion 
Email:
It's working — just type your email and follow the prompts:


Email: dafe.apps@gmail.com
Password: ••••••••
Password (again): ••••••••
Superuser created successfully.
Then visit your admin panel at https://yourdomain.com/admin/ and log in.

also plaform should be associated to organization 1
Let me check the Organization model and membership structure first.

Bash Check Organization model fields and membership
IN
grep -n "class Organization\|class OrganizationMember\|members\|user" /home/rock/Desktop/2026_Projects/my/dafeapp/organizations/models.py | head -40

OUT
10:class Organization(models.Model):
41:        return self.memberships.filter(is_active=True).count()
44:class OrganizationMembership(models.Model):
51:    user = models.ForeignKey(
54:        related_name="memberships",
59:        related_name="memberships",
75:        unique_together = ("user", "organization")
79:        return f"{self.user.email} @ {self.organization.name} [{self.role}]"
82:class OrganizationInvite(models.Model):
122:    def accept(self, user):
123:        """Accept the invite and create membership. Call within transaction.atomic()."""
124:        membership, created = OrganizationMembership.objects.get_or_create(
125:            user=user,
131:            membership.role = self.role
132:            membership.is_active = True
133:            membership.save(update_fields=["role", "is_active"])
136:        return membership
Bash Check role choices on OrganizationMembership
IN
grep -n "role\|ADMIN\|OWNER\|MEMBER" /home/rock/Desktop/2026_Projects/my/dafeapp/organizations/models.py | head -20

OUT
46:        SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
47:        ADMIN = "ADMIN", "Admin"
61:    role = models.CharField(
76:        ordering = ["role", "joined_at"]
79:        return f"{self.user.email} @ {self.organization.name} [{self.role}]"
88:    role = models.CharField(
107:        return f"Invite: {self.email} → {self.organization.name} [{self.role}]"
127:            defaults={"role": self.role, "invited_by": self.created_by},
131:            membership.role = self.role
133:            membership.save(update_fields=["role", "is_active"])
Run this on the droplet:


docker compose -f docker-compose.prod.yml exec web python manage.py shell -c "
from users.models import User
from organizations.models import Organization, OrganizationMembership

user = User.objects.get(email='dafe.apps@gmail.com')
org = Organization.objects.get(id=1)

membership, created = OrganizationMembership.objects.get_or_create(
    user=user,
    organization=org,
    defaults={'role': OrganizationMembership.Role.SUPER_ADMIN, 'is_active': True}
)
if not created:
    membership.role = OrganizationMembership.Role.SUPER_ADMIN
    membership.is_active = True
    membership.save()

print(f'Done: {user.email} → {org.name} [{membership.role}]')
"
This creates the user as SUPER_ADMIN of org 1. If they're already a member it just upgrades their role.


Bash List recent cloud migrations
IN
ls /home/rock/Desktop/2026_Projects/my/dafeapp/cloud/migrations/ | sort | tail -10

OUT
0004_dafeapp_key_auth.py
0005_system_ssh_key.py
0006_remove_ssh_key_auth.py
0007_externalserver_ssh_key_path.py
0008_pyossshsettings.py
0009_cloudaccount_do_auth_method_and_more.py
0010_remove_protect_on_delete.py
0011_platform_cloud_account.py
__init__.py
__pycache__
The migration 0011 exists locally but likely hasn't been run on production. That's the cause of the 500 — the is_platform column doesn't exist in the database yet.

Run this on the droplet:


docker compose -f docker-compose.prod.yml exec web python manage.py migrate
Then check the result:


docker compose -f docker-compose.prod.yml exec web python manage.py showmigrations cloud | tail -5
You should see [X] 0011_platform_cloud_account — then refresh /admin/cloud/cloudaccount/ and the 500 will be gone.

how can i  check logs
On the droplet, run any of these:

Web/Django logs (most useful for 500 errors):


docker compose -f docker-compose.prod.yml logs web --tail=50
Follow live logs (stream in real time):


docker compose -f docker-compose.prod.yml logs web -f
All services at once:


docker compose -f docker-compose.prod.yml logs --tail=30
Celery worker logs:


docker compose -f docker-compose.prod.yml logs celery --tail=50
Filter for errors only:


docker compose -f docker-compose.prod.yml logs web --tail=100 | grep -i "error\|exception\|traceback"
For the 500 you're seeing right now, run:


docker compose -f docker-compose.prod.yml logs web --tail=50
It will show the full Django traceback so you can see exactly what's failing


DO AFTER DEPLOYEMENT
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d web
.




THIS WORKS

docker compose -f docker-compose.prod.yml logs celery_worker --tail=100
docker compose -f docker-compose.prod.yml logs celery_beat --tail=50


how to get live logss

docker compose -f docker-compose.prod.yml logs web -f
The -f flag streams logs live. For all services at once:


docker compose -f docker-compose.prod.yml logs -f
For just celery worker:


docker compose -f docker-compose.prod.yml logs celery_worker -f
Press Ctrl+C to stop streaming.