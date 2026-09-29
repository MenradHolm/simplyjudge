from django import forms

from .models import Competition


class CompetitionEditionForm(forms.Form):
    edition_name = forms.CharField(
        label='Edition name',
        max_length=120,
        help_text="Use a date or cycle name, such as 'October 2026'.",
    )
    slug = forms.SlugField(
        label='URL slug',
        max_length=200,
        required=False,
        help_text='Leave blank to generate this automatically.',
    )

    def __init__(self, *args, series, **kwargs):
        super().__init__(*args, **kwargs)
        self.series = series

    def clean_edition_name(self):
        edition_name = self.cleaned_data['edition_name'].strip()
        if self.series.editions.filter(edition_name__iexact=edition_name).exists():
            raise forms.ValidationError('This series already has an edition with that name.')
        return edition_name

    def clean_slug(self):
        slug = self.cleaned_data.get('slug', '').strip()
        if slug and Competition.objects.filter(slug=slug).exists():
            raise forms.ValidationError('That URL slug is already in use.')
        return slug
