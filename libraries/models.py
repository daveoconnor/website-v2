import re
from typing import Self
from urllib.parse import urlparse

from django.core.cache import caches
from django.db import models, transaction
from django.utils.functional import cached_property
from django.utils.text import slugify

from core.boostrenderer import get_body_from_html
from core.markdown import process_md
from core.models import RenderedContent
from core.tasks import adoc_to_html

from .utils import generate_random_string, write_content_to_tempfile


class Category(models.Model):
    """
    Library categories such as:
      - Math and Numerics
      - Algorithms
      - etc
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(blank=True, null=True)

    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        return super(Category, self).save(*args, **kwargs)


class CommitAuthor(models.Model):
    name = models.CharField(max_length=100)
    avatar_url = models.URLField(null=True, max_length=100)
    github_profile_url = models.URLField(null=True, max_length=100)

    def __str__(self):
        return self.name

    @transaction.atomic
    def merge_author(self, other: Self):
        """Update references to `other` to point to `self`.

        Deletes `other` after updating references.
        """
        if self.pk == other.pk:
            return
        Commit.objects.filter(author=other).update(author=self)
        other.commitauthoremail_set.update(author=self)
        if not self.avatar_url:
            self.avatar_url = other.avatar_url
        if not self.github_profile_url:
            self.github_profile_url = other.github_profile_url
        self.save(update_fields=["avatar_url", "github_profile_url"])
        other.delete()


class CommitAuthorEmail(models.Model):
    author = models.ForeignKey(CommitAuthor, on_delete=models.CASCADE)
    email = models.CharField(unique=True)

    def __str__(self):
        return f"{self.author.name}: {self.email}"


class Commit(models.Model):
    author = models.ForeignKey(CommitAuthor, on_delete=models.CASCADE)
    library_version = models.ForeignKey("LibraryVersion", on_delete=models.CASCADE)
    sha = models.CharField(max_length=40)
    message = models.TextField(default="")
    committed_at = models.DateTimeField(db_index=True)
    is_merge = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["sha", "library_version"],
                name="%(app_label)s_%(class)s_sha_library_version_unique",
            )
        ]

    def __str__(self):
        return self.sha


class Library(models.Model):
    """
    Model to represent component Libraries of Boost

    The Library model is the main model for Boost Libraries. Default values
    come from the .gitmodules file in the main Boost repo, and the libraries.json
    file in the meta/ directory of Boost library repos.

    Most libraries have a single Library object, but some libraries have multiple
    Library objects. For example, the Boost Math library has a Library object
    for multiple sub-libraries. Each of those libraries will be its own Library
    object, and will have the github_url to the main library repo.
    """

    name = models.CharField(
        max_length=100,
        db_index=True,
        help_text="The name of the library as defined in libraries.json.",
    )
    key = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="The key of the library as defined in libraries.json.",
    )
    slug = models.SlugField(
        blank=True, null=True, help_text="The slug of the library, used in the URL."
    )
    description = models.TextField(
        blank=True, null=True, help_text="The description of the library."
    )
    github_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="The URL of the library's GitHub repository.",
    )
    versions = models.ManyToManyField(
        "versions.Version", through="libraries.LibraryVersion", related_name="libraries"
    )
    cpp_standard_minimum = models.CharField(max_length=50, blank=True, null=True)
    active_development = models.BooleanField(default=True, db_index=True)
    categories = models.ManyToManyField(Category, related_name="libraries")

    authors = models.ManyToManyField("users.User", related_name="authors")
    featured = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Should this library be featured on the home page?",
    )
    data = models.JSONField(
        default=dict, help_text="Contains the libraries.json for this library"
    )

    class Meta:
        verbose_name_plural = "Libraries"

    @cached_property
    def display_name(self):
        """Returns the display name for the library."""
        return "Boost." + self.display_name_short

    @cached_property
    def display_name_short(self):
        """Returns the short display name for the library."""

        # Custom method to capitalize words, taking care of special cases
        def custom_capitalize(word):
            # Only capitalize if the word is not already in CamelCase
            if not re.match(r"[A-Z][a-z]+[A-Z][A-Za-z]*", word):
                return "".join(part.capitalize() for part in re.split(r"(/)", word))
            return word

        # Split the name into segments to handle parts inside parentheses separately
        segments = re.split(r"(\([^\)]+\))", self.name)
        processed_segments = []

        for segment in segments:
            # Check if the segment is within parentheses
            if segment.startswith("(") and segment.endswith(")"):
                # Process the content within parentheses without the surrounding ()
                inner_content = segment[1:-1]
                processed_segments.append(f"({custom_capitalize(inner_content)})")
            else:
                # Split on whitespace, hyphens, underscores for regular segments
                words = re.split(r"[\s\-_]+", segment)
                capitalized_words = [custom_capitalize(word) for word in words]
                processed_segments.append("".join(capitalized_words))

        return "".join(processed_segments)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """Override the save method to confirm the slug is set (or set it)

        We need the slug to be unique, but we want to intelligently make that happen,
        because there are libraries (like Container Hash) that are more easily managed
        as two records due to changes in the data between versions.
        """
        # Generate slug based on name
        if not self.slug:
            # Base the slug name off of the key from the gitmodules file.
            slug = slugify(self.key)

            # If there is a library with that slug, try a slug based on the key from the
            # gitmodules file
            if Library.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = slugify(self.key)

            # If that slug already exists, append a random string to the slug
            if Library.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                random_str = generate_random_string()
                slug = f"{slug}-{random_str}"

            self.slug = slug
        return super().save(*args, **kwargs)

    def get_description(self, client, tag="develop"):
        """Get description from the appropriate file on GitHub.

        For more recent versions, that will be `/doc/library-details.adoc`.
        For older versions, or libraries that have not adopted the adoc file,
        that will be `/README.md`.
        """
        content = None
        # File paths/names where description data might be stored.
        files = ["doc/library-detail.adoc", "README.md"]

        # Try to get the content from the cache first
        static_content_cache = caches["static_content"]
        cache_key = f"library_description_{self.github_repo}_{tag}"
        cached_result = static_content_cache.get(cache_key)
        if cached_result:
            return cached_result

        # Now try to get the content from the database
        try:
            content_obj = RenderedContent.objects.get(cache_key=cache_key)
            # TODO: if master or develop, fire a task to update the content
            return content_obj.content_html
        except RenderedContent.DoesNotExist:
            pass

        # It's not in a cache -- now try to get the content of each file in turn
        for file_path in files:
            content = client.get_file_content(
                repo_slug=self.github_repo, tag=tag, file_path=file_path
            )
            if content:
                # There is content, so process it
                temp_file = write_content_to_tempfile(content)
                if file_path.endswith(".adoc"):
                    html_content = adoc_to_html(temp_file.name, delete_file=True)
                    body_content = get_body_from_html(html_content)
                else:
                    _, body_content = process_md(temp_file.name)
                static_content_cache.set(cache_key, body_content)
                RenderedContent.objects.update_or_create(
                    cache_key=cache_key,
                    content_html=body_content,
                    content_type="text/html",
                )
                return body_content

        # If no content was found for any of the files
        return None

    def get_cpp_standard_minimum_display(self):
        """Returns the display name for the C++ standard, or the value if not found.

        Source of values is
        https://docs.cppalliance.org/user-guide/prev/library_metadata.html"""
        display_names = {
            "98": "C++98",
            "03": "C++03",
            "11": "C++11",
            "14": "C++14",
            "17": "C++17",
            "20": "C++20",
        }
        return display_names.get(self.cpp_standard_minimum, self.cpp_standard_minimum)

    def github_properties(self):
        """Returns the owner and repo name for the library"""
        if not self.github_url:
            return {}

        parts = urlparse(self.github_url)
        path = parts.path.split("/")

        owner = path[1]
        repo = path[2]

        return {
            "owner": owner,
            "repo": repo,
        }

    @cached_property
    def first_boost_version(self):
        """Returns the first Boost version that included this library"""
        if not self.library_version.exists():
            return
        return (
            self.library_version.order_by("version__release_date", "version__name")
            .first()
            .version
        )

    @cached_property
    def github_owner(self):
        """Returns the name of the GitHub owner for the library"""
        return self.github_properties().get("owner")

    @cached_property
    def github_repo(self):
        """Returns the name of the GitHub repository for the library"""
        return self.github_properties().get("repo")

    @cached_property
    def github_issues_url(self):
        """
        Returns the URL to the GitHub issues page for the library

        Does not check if the URL is valid.
        """
        if not self.github_owner or not self.github_repo:
            raise ValueError("Invalid GitHub owner or repository")

        return f"https://github.com/{self.github_owner}/{self.github_repo}/issues"


class LibraryVersion(models.Model):
    version = models.ForeignKey(
        "versions.Version",
        related_name="library_version",
        on_delete=models.CASCADE,
    )
    library = models.ForeignKey(
        "libraries.Library",
        related_name="library_version",
        on_delete=models.CASCADE,
    )
    maintainers = models.ManyToManyField("users.User", related_name="maintainers")
    missing_docs = models.BooleanField(
        default=False,
        help_text="If true, then there are not docs for this version of this library.",
    )
    documentation_url = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="The path to the docs for this library version.",
    )
    data = models.JSONField(
        default=dict, help_text="Contains the libraries.json for this library-version"
    )

    def __str__(self):
        return f"{self.library.name} ({self.version.name})"

    @cached_property
    def library_repo_url_for_version(self):
        """Returns the URL to the GitHub repository for the library at this specicfic
        version.
        """
        if not self.library or not self.version or not self.library.github_url:
            raise ValueError("Invalid data for library version")

        return f"{self.library.github_url}/tree/{self.version.name}"


class Issue(models.Model):
    """
    Model that tracks Library repository issues in Github
    """

    library = models.ForeignKey(
        Library, related_name="issues", on_delete=models.CASCADE
    )
    title = models.CharField(max_length=255)
    number = models.IntegerField()
    github_id = models.CharField(max_length=100, db_index=True)
    is_open = models.BooleanField(default=False, db_index=True)
    closed = models.DateTimeField(blank=True, null=True, db_index=True)

    created = models.DateTimeField(db_index=True)
    modified = models.DateTimeField(db_index=True)

    data = models.JSONField(default=dict)

    def __str__(self):
        return f"({self.number}) - {self.title}"


class PullRequest(models.Model):
    """
    Model that tracks Pull Requests in Github for a Library
    """

    library = models.ForeignKey(
        Library, related_name="pull_requests", on_delete=models.CASCADE
    )

    title = models.CharField(max_length=255)
    number = models.IntegerField()
    github_id = models.CharField(max_length=100, db_index=True)
    is_open = models.BooleanField(default=False, db_index=True)
    closed = models.DateTimeField(blank=True, null=True, db_index=True)
    merged = models.DateTimeField(blank=True, null=True, db_index=True)

    created = models.DateTimeField(db_index=True)
    modified = models.DateTimeField(db_index=True)

    data = models.JSONField(default=dict)

    def __str__(self):
        return f"({self.number}) - {self.title}"
