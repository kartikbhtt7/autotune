# Generated by Django 4.2.13 on 2024-07-01 04:55

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0027_mlmodelconfig_alter_workflows_llm_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="mlmodel",
            name="config",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="models",
                to="workflow.mlmodelconfig",
            ),
        ),
    ]
