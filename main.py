import html
import json
import re
import os
import http.client
import ssl
import socket
import urllib.request

from datetime import datetime, timezone
from time import sleep
from typing import Union, Optional, List, Dict

# decorators 
def retry(num_retries=3, delay=1, exceptions=(Exception,)):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for i in range(num_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    print(e)
                    sleep(delay)
                print(f'Retrying attempt {i+2}/{num_retries}')
        return wrapper
    return decorator

def ignore_exceptions(exceptions=(Exception,)):
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except exceptions as e:
                return None
        return wrapper
    return decorator

MAX_PER_PAGE = 100
MAX_ITERATIONS = 100

# exceptions
class UnauthrorizedError(Exception):
    pass

class UnauthenticatedError(Exception):
    pass

class ResourceDoesNotExistError(Exception):
    pass

class HttpResponse:
    def __init__(self, response: http.client.HTTPResponse, data: bytes):
        self._response = response
        self._data = data
        self.status_code = response.status
        self.reason = response.reason
        self.headers = dict(response.headers)
    
    def json(self) -> Dict:
        return json.loads(self._data.decode('utf-8'))
    
    @property
    def text(self) -> str:
        return self._data.decode('utf-8')
    
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}: {self.reason}")


class HttpSession:
    def __init__(self):
        self.connections = {}  # host:port:ssl -> connection
        self.headers = {}
        self.cookies = {}
        self.timeout = 30
        
    def __del__(self):
        try:
            self.close()
        except:
            pass
    
    def _get_connection(self, host: str, port: int = 443):
        """Get or create a persistent connection"""
        conn_key = f"{host}:{port}"
        
        # check if existing connection is still valid
        if conn_key in self.connections:
            conn = self.connections[conn_key]
            try:
                # if connection has a socket, assume it's alive
                if hasattr(conn, 'sock') and conn.sock:
                    return conn
            except:
                pass
            
            try:
                conn.close()
            except:
                pass
            del self.connections[conn_key]
        
        # Create new connection
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(
            host, port, 
            timeout=self.timeout,
            context=context
        )
        
        try:
            conn.connect()
            self.connections[conn_key] = conn
            return conn
        except Exception as e:
            raise Exception(f"Failed to connect to {host}:{port}: {e}")
    
    def request(self, method: str, url: str, max_redirects: int = 10, **kwargs) -> HttpResponse:
        """Make HTTP request with connection pooling and redirect following"""
        redirects = 0
        
        while redirects < max_redirects:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc
            path = parsed.path
            if parsed.query:
                path += f"?{parsed.query}"
            
            use_ssl = parsed.scheme == 'https'
            port = parsed.port or (443 if use_ssl else 80)
            
            headers = self.headers.copy()
            if 'headers' in kwargs:
                headers.update(kwargs['headers'])
            
            if self.cookies:
                cookie_str = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
                headers['Cookie'] = cookie_str
            
            conn = self._get_connection(host, port)
            
            try:
                conn.request(method, path, headers=headers)
                response = conn.getresponse()
                data = response.read()
                
                # Check for redirects
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if location:
                        # Handle relative URLs
                        if not location.startswith('http'):
                            location = urllib.parse.urljoin(url, location)
                        url = location
                        redirects += 1
                        # For 303, change method to GET
                        if response.status == 303:
                            method = 'GET'
                        continue
                
                return HttpResponse(response, data)
                
            except (socket.timeout, socket.error, http.client.HTTPException) as e:
                conn_key = f"{host}:{port}:{use_ssl}"
                if conn_key in self.connections:
                    try:
                        self.connections[conn_key].close()
                    except:
                        pass
                    del self.connections[conn_key]
                raise
        
        raise Exception(f"Too many redirects (>{max_redirects})")
    
    def get(self, url: str, **kwargs) -> HttpResponse:
        return self.request('GET', url, **kwargs)
    
    def close(self):
        """Close all connections"""
        for conn in self.connections.values():
            try:
                conn.close()
            except:
                pass
        self.connections.clear()

class CanvasClient:
    def __init__(self, base_url: str, canvas_session: str) -> None:
        """
        :param base_url: The base URL of the Canvas instance.
        :param canvas_session: The API key to authenticate requests with.
        """
        if "api/v1" in base_url:
            raise ValueError(
                "`base_url` should not specify an API version. Remove trailing /api/v1/"
            )
        assert 'https://' in base_url, "Canvas base_url must start with 'https://'"
        assert base_url.strip(), "Canvas base_url must be a non-blank string"

        self.base_url = base_url.strip().rstrip('/')
        self.api_url = base_url.strip().rstrip('/') + '/api/v1'
        self.canvas_session = canvas_session.strip()
        
        self.session = HttpSession()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
            'Content-Type': 'application/json'
        })
        if self.canvas_session:
            self.session.cookies['canvas_session'] = self.canvas_session

    @retry(num_retries=3, delay=2, exceptions=(socket.timeout, ))
    def _request(self, endpoint: str, params: Optional[Dict] = None, use_api_url: bool = True) -> Dict:
        if not use_api_url:
            url = f"{self.base_url}/{endpoint}"
            response = self.session.get(url)
            return response.text
        
        url = f"{self.api_url}/{endpoint}"
        
        # Check if pagination is requested
        should_paginate = params and 'per_page' in params and params.get('per_page') == MAX_PER_PAGE
        
        if should_paginate:
            return self._paginated_request(url, params)
        else:
            # Single request without pagination
            if params:
                query_string = urllib.parse.urlencode(params, doseq=True)
                url = f"{url}?{query_string}"
            
            return self._make_single_request(url)

    def _make_single_request(self, url: str) -> Dict:
        """Make a single HTTP request and return JSON response."""
        try:
            response = self.session.get(url)
            
            if response.status_code >= 400:
                try:
                    return response.json()
                except:
                    return {'error': f'HTTP {response.status_code}: {response.reason}'}
            
            return response.json()
            
        except socket.timeout:
            print(f"Request timeout for URL: {url}")
            raise
        except Exception as e:
            print(f"Request error for URL: {url} - {e}")
            raise

    def _paginated_request(self, url: str, params: Dict) -> List[Dict]:
        """Make paginated requests and aggregate results."""
        all_results = []
        
        for page in range(1, MAX_ITERATIONS + 1):
            page_params = params.copy()
            page_params['page'] = page
            
            query_string = urllib.parse.urlencode(page_params, doseq=True)
            page_url = f"{url}?{query_string}"
            
            try:
                response = self.session.get(page_url)
                
                if response.status_code >= 400:
                    if page == 1:
                        try:
                            return response.json()
                        except:
                            return {'error': f'HTTP {response.status_code}: {response.reason}'}
                    break
                
                page_data = response.json()
                
                # If response is a dict (error or single object), handle it
                if isinstance(page_data, dict):
                    return page_data if page == 1 else all_results
                
                # If empty list, we've reached the end
                if not page_data:
                    break
                
                all_results.extend(page_data)
                
                # If fewer results than per_page, we've reached the end
                if len(page_data) < MAX_PER_PAGE:
                    break
                    
            except socket.timeout:
                print(f"Request timeout for URL: {page_url}")
                if page == 1:
                    raise
                break
            except Exception as e:
                print(f"Request error for URL: {page_url} - {e}")
                if page == 1:
                    raise
                break
        
        return all_results

    def get_assignment(self, assignment_id: Union[int, str], course_id: Union[int, str], **kwargs) -> Dict:
        """
        Return a single assignment.

        :param assignment_id: The ID of the assignment to retrieve.
        :param course_id: The ID of the course the assignment belongs to.
        """
        response = self._request(f"courses/{course_id}/assignments/{assignment_id}", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access assignment ID: {assignment_id} for course ID: {course_id}')
        return response

    def get_assignments(self, course_id: Union[int, str], **kwargs) -> List[Dict]:
        """
        Return the list of assignments for the specified course.

        :param course_id: The ID of the course to retrieve assignments from.
        """
        response = self._request(f"courses/{course_id}/assignments", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access assignments for course ID: {course_id}')
        return response
    
    def get_assignment_groups(self, course_id: Union[int, str], **kwargs) -> List[Dict]:
        """
        Return the list of assignment groups for the specified course.

        :param course_id: The ID of the course to retrieve assignment groups from.
        """
        return self._request(f"courses/{course_id}/assignment_groups", params=kwargs)
    
    def get_announcements(self, course_id: int, **kwargs) -> List[Dict]:
        """
        List announcements.

        :param context_codes: Course ID(s) to request announcements from.
        """
        kwargs['context_codes'] = [f'course_{course_id}']
        kwargs['start_date'] = kwargs.get('start_date', '1900-01-01')
        kwargs['end_date'] = kwargs.get('end_date', datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
        kwargs['per_page'] = kwargs.get('per_page', MAX_PER_PAGE)
        kwargs['page'] = kwargs.get('page', 1)
        return self._request("announcements", params=kwargs)

    def get_course(self, course_id: Union[int, str], **kwargs) -> Dict:
        """
        Retrieve a course by its ID.

        :param course_id: The ID of the course to retrieve.
        """
        response =  self._request(f"courses/{course_id}", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access course ID: {course_id}')
        if isinstance(response, dict) and response.get('status') == 'unauthorized':
            raise UnauthrorizedError(f'Unauthorized to access course ID: {course_id}')
        return response

    def get_courses(self, **kwargs) -> List[Dict]:
        """
        Return a list of active courses for the current user.
        """
        return self._request("courses", params=kwargs)

    def get_file(self, file_id: Union[int, str], course_id: Union[int, str] = None, **kwargs) -> Dict:
        """
        Return the standard attachment json object for a file.

        :param file_id: The ID of the file to retrieve.
        """
        if course_id is None:
            response = self._request(f"files/{file_id}", params=kwargs)
        
        response = self._request(f"courses/{course_id}/files/{file_id}", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthorized':
            raise UnauthrorizedError(f'Unauthorized to access /courses/{course_id}/files/{file_id}')
        elif isinstance(response, dict) and response.get('errors', '') == [{'message': 'The specified resource does not exist.'}]:
            raise ResourceDoesNotExistError(f'Resource does not exist for /courses/{course_id}/files/{file_id}')
        return response
    
    def get_files(self, course_id: Union[int, str], **kwargs) -> List[Dict]:
        """
        Return the list of files for the specified course.

        :param course_id: The ID of the course to retrieve files from.
        """
        response = self._request(f"courses/{course_id}/files", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access /courses/{course_id}/files')
        if isinstance(response, dict) and response.get('status') == 'unauthorized':
            raise UnauthrorizedError(f'Unauthorized to access /courses/{course_id}/files')
        return response

    def get_folder(self, folder_id: Union[int, str], **kwargs) -> Dict:
        """
        Return the details for a folder.

        :param folder_id: The ID of the folder to retrieve.
        """
        return self._request(f"folders/{folder_id}", params=kwargs)

    def get_folders(self, course_id: Union[int, str], **kwargs) -> List[Dict]:
        """
        Return the list of folders for the specified course.

        :param course_id: The ID of the course to retrieve folders from.
        """
        response = self._request(f"courses/{course_id}/folders", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access /courses/{course_id}/folders')
        if isinstance(response, dict) and response.get('status') == 'unauthorized':
            raise UnauthrorizedError(f'Unauthorized to access /courses/{course_id}/folders')
        return response

    def get_folder_files(self, folder_id: Union[int, str], **kwargs) -> List[Dict]:
        """
        Return the list of files for the specified folder.

        :param folder_id: The ID of the folder to retrieve files from.
        """
        response = self._request(f"folders/{folder_id}/files", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access /folders/{folder_id}/files')
        if isinstance(response, dict) and response.get('status') == 'unauthorized':
            raise UnauthrorizedError(f'Unauthorized to access /folders/{folder_id}/files')
        return response

    def get_module(self, module_id: Union[int, str], course_id: Union[int, str], **kwargs) -> Dict:
        """
        Return a single module.

        :param module_id: The ID of the module to retrieve.
        :param course_id: The ID of the course the module belongs to.
        """
        return self._request(f"courses/{course_id}/modules/{module_id}", params=kwargs)

    def get_module_items(self, module_id: Union[int, str], course_id: Union[int, str], **kwargs) -> List[Dict]:
        """
        Return the list of items in a module.

        :param module_id: The ID of the module to retrieve items from.
        :param course_id: The ID of the course the module belongs to.
        """
        return self._request(f"courses/{course_id}/modules/{module_id}/items", params=kwargs)

    def get_modules(self, course_id: Union[int, str], **kwargs) -> List[Dict]:
        """
        Return a list of modules in this course.

        :param course_id: The ID of the course to retrieve modules from.
        """
        response = self._request(f"courses/{course_id}/modules", params=kwargs)
        if isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access /courses/{course_id}/modules')
        return response

    def get_page(self, page_id: Union[int, str], course_id: Union[int, str], **kwargs) -> Dict:
        """
        Retrieve a page by its URL name.

        :param page_id: The URL name of the page to retrieve.
        :param course_id: The ID of the course the page belongs to.
        """
        return self._request(f"courses/{course_id}/pages/{page_id}", params=kwargs)
    
    def get_pages(self, course_id: Union[int, str], **kwargs) -> List[Dict] | Dict:
        """
        List the pages in a course.

        :param course_id: The ID of the course to retrieve pages from.
        """
        response = self._request(f"courses/{course_id}/pages", params=kwargs)
        if isinstance(response, dict) and response.get('message', '') == 'That page has been disabled for this course':
            raise ResourceDoesNotExistError(f'Pages have been disabled for course ID: {course_id}')
        elif isinstance(response, dict) and len(response.keys()) == 1 and response.get('message') != '':
            raise ResourceDoesNotExistError(f'Error retrieving pages for course ID: {course_id}: {response.get("message")}')
        elif isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access /courses/{course_id}/pages')
        return response

    def show_front_page(self, course_id: Union[int, str], **kwargs) -> Dict:
        """
        Retrieve the content of the front page.

        :param course_id: The ID of the course to retrieve the front page from.
        """
        response = self._request(f"courses/{course_id}/front_page", params=kwargs)
        if isinstance(response, dict) and response.get('message', '') == 'No front page has been set':
            raise ResourceDoesNotExistError(f'No front page has been set for course ID: {course_id}')
        elif isinstance(response, dict) and response.get('status') == 'unauthenticated':
            raise UnauthenticatedError(f'Unauthenticated to access /courses/{course_id}/front_page')
        return response
    



# Helper functions
sanitize = lambda s: re.sub(r'[\\/:"*?<>|]+', '_', s)
extract_files = lambda text: set(
    re.findall(r'/courses/\d+/files/(\d+)', text, re.IGNORECASE)
)
def write_to_file(path: str, content: str | bytes | List | Dict) -> None:
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        if isinstance(content, dict) or isinstance(content, list):
            json.dump(content, f, indent=4)
        else:
            f.write(content)

def write_with_streaming(path: str, url: str, opener: urllib.request.OpenerDirector) -> None:
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    
    req = urllib.request.Request(url)
    
    with opener.open(req) as response:
        with open(path, 'wb') as f:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                f.write(chunk)

def download_files(file_list: List[Dict], output_folder: str, opener: urllib.request.OpenerDirector) -> None:
    os.makedirs(output_folder, exist_ok=True)
    for file in file_list:
        display_name = file.get('name', f'File_{file["id"]}')
        print(f'Downloading File: {display_name} from {file["download_url"]}')
        write_with_streaming(os.path.join(output_folder, sanitize(display_name)), file['download_url'], opener)

class CanvasCourseScraper:
    def __init__(self, 
                 canvas: CanvasClient, 
                 course_id: Union[int, str]) -> None:
        self.canvas = canvas
        self.course_id = course_id
        self.files_downloaded = set()
        self.files: List[Dict] = []
    
    def _try_api(self, api_func, fallback_frontend_func = None) -> Union[List[Dict], None]:
        try:
            return api_func()
        except UnauthenticatedError:
            if fallback_frontend_func:
                fallback_frontend_func()
        return None

    @ignore_exceptions((UnauthrorizedError, ResourceDoesNotExistError))
    def scrape_file(self, file_id: Union[int, str]) -> None:
        file_id = int(file_id)
        if file_id in self.files_downloaded:
            return
        file = self.canvas.get_file(file_id, self.course_id)
        self.files.append(
            {
                'id': file_id,
                'folder_id': file.get('folder_id', None),
                'name': file.get('display_name', file.get('filename', f'File_{file_id}')),
                'created_at': file.get('created_at', None),
                'updated_at': file.get('updated_at', None),
                'size': file.get('size', None),
                'download_url': file.get('url', None),
            }
        )
        print(f'Found File: {file.get("display_name", file.get("filename", f"File_{file_id}"))} with ID: {file_id}')
        self.files_downloaded.add(file_id)
    
    def scrape_files(self, file_ids: List[Union[int, str]]) -> None:
        if not file_ids:
            return
        for file_id in file_ids:
            self.scrape_file(file_id)
    
    def scrape_html_content(self, html: str) -> None:
        file_ids = extract_files(html)
        self.scrape_files(file_ids)
    
    def scrape_external_url(self, item: Dict) -> None:
        pass
    
    def scrape_page(self, page_id: Union[int, str]) -> None:
        page = self.canvas.get_page(page_id, self.course_id)
        if not page:
            return
        self.scrape_html_content(
            page.get('body', '') or ''
        )
    
    @ignore_exceptions((ResourceDoesNotExistError,))
    def scrape_pages(self) -> None:
        pages = self._try_api(
            lambda: self.canvas.get_pages(self.course_id, **{'per_page': MAX_PER_PAGE}),
            fallback_frontend_func=lambda: print('Unauthenticated to access pages (API only, requires login).')
        )
        if not pages:
            return
        for page in pages:
            self.scrape_page(page['page_id'])
    
    def scrape_assignment(self, assignment_id: Union[int, str]) -> None:
        assignment = self._try_api(
            lambda: self.canvas.get_assignment(assignment_id, self.course_id),
            fallback_frontend_func=lambda: self.scrape_assignment_frontend(assignment_id)
        )
        if not assignment:
            return
        description = assignment.get('description', '') or ''
        html_content = ''
        html_url = assignment.get('html_url')
        if html_url:
            response = self.canvas.session.get(html_url)
            if response.status_code == 200:
                html_content = response.text
        self.scrape_html_content(
            description + html_content
        )

    def scrape_assignment_frontend(self, assignment_id: Union[int, str]) -> None:
        """
        For public courses.  Auth-required courses will invoke https://canvas.cornell.edu/api/graphql
        for assignment files and user submissions
        """
        response = self.canvas._request(f"courses/{self.course_id}/assignments/{assignment_id}", use_api_url=False)
        self.scrape_html_content(response)

    def scrape_assignments(self) -> None:
        assignments = self._try_api(
            lambda: self.canvas.get_assignments(self.course_id, **{'per_page': MAX_PER_PAGE}),
            fallback_frontend_func=lambda: print('Unauthenticated to access assignments (API only, requires login).')
        )
        if not assignments:
            return
        for assignment in assignments:
            self.scrape_assignment(assignment['id'])
    
    def scrape_assignment_groups(self) -> None:
        assignment_groups = self.canvas.get_assignment_groups(
            self.course_id,
            **{
                'exclude_assignment_submission_types[]': 'wiki_page',
                'exclude_response_fields[]': ['description', 'rubric'],
                'include[]': ['assignments', 'discussion_topic'],
                'override_assignment_dates': True,
                'per_page': MAX_PER_PAGE
            }
        )
        if not assignment_groups:
            return
        for group in assignment_groups:
            for assignment in group.get('assignments', []):
                self.scrape_assignment(assignment['id'])

    
    def scrape_module(self, module_id: Union[int, str]) -> None:
        items = self.canvas.get_module_items(module_id, self.course_id, **{'per_page': MAX_PER_PAGE})
        for item in items:
            if item['type'] == 'File':
                self.scrape_file(item['content_id'])
            elif item['type'] == 'Page':
                self.scrape_page(item['page_url'])
            elif item['type'] == 'Assignment':
                self.scrape_assignment(item['content_id'])
            elif item['type'] == 'ExternalUrl':
                self.scrape_external_url(item)
            elif item['type'] in ('Quiz', 'SubHeader'):
                print(f'Skipping item type: {item["type"]} with title: {item["title"]}')
            else:
                print(f'Unknown type: {item["type"]}')

    def scrape_modules_frontend(self):
        """
        For public courses.  Modules endpoint is SSR.
        """
        response = self.canvas._request(f"courses/{self.course_id}/modules", use_api_url=False)
        parsed = html.unescape(response)
        item_urls = re.findall(r'href="([^"]+/modules/items/\d+)"', parsed)
        for url in item_urls:
            response = self.canvas._request(url, use_api_url=False)
            self.scrape_html_content(response)
    
    def scrape_modules(self) -> None:
        modules = self._try_api(
            lambda: self.canvas.get_modules(self.course_id, **{'per_page': MAX_PER_PAGE}),
            fallback_frontend_func=lambda: self.scrape_modules_frontend()
        )
        if not modules:
            return
        for module in modules:
            self.scrape_module(module['id'])
    
    @ignore_exceptions((UnauthrorizedError, UnauthenticatedError))
    def scrape_remaining_files(self) -> None:
        files = self.canvas.get_files(self.course_id)
        file_ids = [file['id'] for file in files]
        remaining_file_ids = set(file_ids) - self.files_downloaded
        self.scrape_files(list(remaining_file_ids))
    
    @ignore_exceptions((UnauthrorizedError, UnauthenticatedError))
    def scrape_folder(self, folder: dict) -> None:
        folder_id = re.search(r'/folders/(\d+)/files', folder['files_url'])
        files = self.canvas.get_folder_files(folder_id.group(1))
        file_ids = [file['id'] for file in files]
        self.scrape_files(file_ids)

    @ignore_exceptions((UnauthrorizedError, UnauthenticatedError))
    def scrape_folders(self) -> None:
        folders = self.canvas.get_folders(self.course_id)
        if not folders:
            return
        for folder in folders:
            self.scrape_folder(folder)

    def scrape_syllabus(self) -> None:
        syllabus = self.canvas.get_course(self.course_id, **{'include[]': 'syllabus_body'})
        if not syllabus:
            return
        self.scrape_html_content(syllabus.get('syllabus_body', '') or '')
    
    def scrape_front_page_frontend(self) -> None:
        response = self.canvas._request(f"courses/{self.course_id}", use_api_url=False)
        self.scrape_html_content(response)

    @ignore_exceptions((ResourceDoesNotExistError,))
    def scrape_front_page(self) -> None:
        front_page = self._try_api(
            lambda: self.canvas.show_front_page(self.course_id),
            fallback_frontend_func=lambda: self.scrape_front_page_frontend()
        )
        
        if not front_page:
            return
        self.scrape_html_content(front_page.get('body', '') or '')
    
    def scrape_course(self) -> 'CanvasCourseScraper':
        course = self.canvas.get_course(self.course_id)
        if not course:
            return self
        course_name = sanitize(course.get('name', f'MISC_{course["id"]}'))
        print(f'********* Course: {course_name} ({course["id"]}) *********')
        print(f'********* Modules *********')
        self.scrape_modules()
        print(f'********* Assignments *********')
        self.scrape_assignments()
        print(f'********* Assignment Groups *********')
        self.scrape_assignment_groups()
        print(f'********* Folders *********')
        self.scrape_folders()
        print(f'********* Syllabus *********')
        self.scrape_syllabus()
        print(f'********* Pages *********')
        self.scrape_pages()
        print(f'********* Front Page *********')
        self.scrape_front_page()
        print(f'********* Remaining Files *********')
        self.scrape_remaining_files()
        return self
        




def main():
    import argparse
    from urllib.parse import urlparse
    def validate_url(url):
        try:
            parsed_url = urlparse(url)
            if not all([parsed_url.scheme, parsed_url.netloc]):
                raise argparse.ArgumentTypeError("Invalid URL format")
        except ValueError:
            raise argparse.ArgumentTypeError("Invalid URL format")
        
        pattern = re.compile(r'courses/(\d+)')
        match = pattern.search(url)
        if not match:
            raise argparse.ArgumentTypeError("URL does not contain a valid course ID (e.g. /courses/123)")
        return url

    env_canvas_session = os.environ.get('CANVAS_SESSION')

    parser = argparse.ArgumentParser(description='Canvas course downloader')
    parser.add_argument('-u', '--url', 
                        type=validate_url,
                        required=True, 
                        help='The Course URL, e.g. https://canvas.example.edu/courses/123')
    parser.add_argument('-s', '--canvas-session', 
                        type=str, 
                        required=False, 
                        default=env_canvas_session,
                        help='The Canvas API canvas session, provided as an environment variable or command line argument.  If not provided, the script will use the CANVAS_SESSION environment variable.')
    parser.add_argument('-o', '--output-folder', 
                        type=str, 
                        default='output',
                        help='The output folder to save files to (default "output")')
    parser.add_argument('-d', '--download-files',
                        action='store_true',
                        help='Download the files found (default is to only list them)')
    
    args = parser.parse_args()
    course_id = re.search(r'courses/(\d+)', args.url).group(1)
    canvas_session = '' if args.canvas_session is None else args.canvas_session

    canvas = CanvasClient(
        base_url=re.sub(r'/courses/\d+.*$', '', args.url),
        canvas_session=canvas_session
    )
    try:
        course_name = sanitize(canvas.get_course(course_id).get('name', f'Course_{course_id}'))
    except UnauthenticatedError as e:
        print('Error: Unauthenticated. Your course requires login. Please check your CANVAS_SESSION value. You either entered an invalid value or your session has expired.')
        return
    output_folder = os.path.join(args.output_folder, f'{course_name}_{course_id}')
    scraper = CanvasCourseScraper(canvas, course_id).scrape_course()
    print(f'Total files found: {len(scraper.files)}')
    if args.download_files and scraper.files:
        opener = urllib.request.build_opener()
        opener.addheaders = [('Cookie', f'canvas_session={canvas_session}')]
        opener.addheaders.append(('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'))
        download_files(scraper.files, os.path.join(output_folder, 'files'), opener)
    write_to_file(os.path.join(output_folder, 'files.json'), scraper.files)


if __name__ == '__main__':
    main()

