# The fork's 0018_gauntlet and upstream's 0018/0019 both follow 0017: this joins the two lines.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0018_gauntlet'),
        ('OpenBench', '0019_test_isa_uarch_regex'),
    ]

    operations = []
