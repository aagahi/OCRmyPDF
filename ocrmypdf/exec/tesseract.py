#!/usr/bin/env python3
# © 2015 James R. Barlow: github.com/jbarlow83

import sys
import os
import re
import shutil
from functools import lru_cache
from ..exceptions import MissingDependencyError, TesseractConfigError
from ..helpers import page_number
from . import get_program
from collections import namedtuple
from textwrap import dedent
import PyPDF2 as pypdf

from subprocess import Popen, PIPE, CalledProcessError, \
    TimeoutExpired, check_output, STDOUT, DEVNULL


OrientationConfidence = namedtuple(
    'OrientationConfidence',
    ('angle', 'confidence'))

HOCR_TEMPLATE = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
 <head>
  <title></title>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
  <meta name='ocr-system' content='tesseract 3.02.02' />
  <meta name='ocr-capabilities' content='ocr_page ocr_carea ocr_par ocr_line ocrx_word'/>
 </head>
 <body>
  <div class='ocr_page' id='page_1' title='image "x.tif"; bbox 0 0 {0} {1}; ppageno 0'>
   <div class='ocr_carea' id='block_1_1' title="bbox 0 1 {0} {1}">
    <p class='ocr_par' dir='ltr' id='par_1' title="bbox 0 1 {0} {1}">
     <span class='ocr_line' id='line_1' title="bbox 0 1 {0} {1}"><span class='ocrx_word' id='word_1' title="bbox 0 1 {0} {1}"> </span>
     </span>
    </p>
   </div>
  </div>
 </body>
</html>'''


@lru_cache(maxsize=1)
def version():
    args_tess = [
        get_program('tesseract'),
        '--version'
    ]
    try:
        versions = check_output(
                args_tess, close_fds=True, universal_newlines=True,
                stderr=STDOUT)
    except CalledProcessError as e:
        print("Could not find Tesseract executable on system PATH.",
              file=sys.stderr)
        raise MissingDependencyError from e

    tesseract_version = re.match(r'tesseract\s(.+)', versions).group(1)
    return tesseract_version


def v4():
    "Is this Tesseract v4.0?"
    return (version() >= '4')


def has_textonly_pdf():
    if version() == '4.00.00alpha':
        # textonly_pdf added during the 4.00.00alpha cycle, so we must test
        # more carefully to see if it is present
        args_tess = [
            get_program('tesseract'),
            '--print-parameters'
        ]
        try:
            params = check_output(
                    args_tess, close_fds=True, universal_newlines=True,
                    stderr=STDOUT)
        except CalledProcessError as e:
            print("Could not --print-parameters from tesseract",
                  file=sys.stderr)
            raise MissingDependencyError from e
        if 'textonly_pdf' in params:
            return True
    else:
        return v4()


def psm():
    "If Tesseract 4.0, use argument --psm instead of -psm"
    return '--psm' if v4() else '-psm'


@lru_cache(maxsize=1)
def languages():
    args_tess = [
        get_program('tesseract'),
        '--list-langs'
    ]
    try:
        langs = check_output(
                args_tess, close_fds=True, universal_newlines=True,
                stderr=STDOUT)
    except CalledProcessError as e:
        msg = dedent("""Tesseract failed to report available languages.
        Output from Tesseract:
        -----------
        """)
        msg += e.output
        print(msg, file=sys.stderr)
        raise MissingDependencyError from e
    return set(lang.strip() for lang in langs.splitlines()[1:])


def tess_base_args(languages, engine_mode):
    args = [
        get_program('tesseract'),
    ]
    if languages:
        args.extend(['-l', '+'.join(languages)])
    if engine_mode is not None and v4():
        args.extend(['--oem', str(engine_mode)])
    return args


def get_orientation(input_file, language: list, engine_mode, timeout: float,
                    log):
    args_tesseract = tess_base_args(language, engine_mode) + [
        psm(), '0',
        input_file,
        'stdout'
    ]

    try:
        stdout = check_output(
            args_tesseract, close_fds=True, stderr=STDOUT,
            universal_newlines=True, timeout=timeout)
    except TimeoutExpired:
        return OrientationConfidence(angle=0, confidence=0.0)
    except CalledProcessError as e:
        tesseract_log_output(log, e.output, input_file)
        if ('Too few characters. Skipping this page' in e.output or
                'Image too large' in e.output):
            return OrientationConfidence(0, 0)
        raise e from e
    else:
        osd = {}
        for line in stdout.splitlines():
            line = line.strip()
            parts = line.split(':', maxsplit=2)
            if len(parts) == 2:
                osd[parts[0].strip()] = parts[1].strip()

        angle = int(osd.get('Orientation in degrees', 0))
        if 'Orientation' in osd:
            # Tesseract < 3.04.01
            # reports "Orientation in degrees" as a counterclockwise angle
            # We keep it clockwise
            assert 'Rotate' not in osd
            angle = -angle % 360
        else:
            # Tesseract == 3.04.01, hopefully also Tesseract > 3.04.01
            # reports "Orientation in degrees" as a clockwise angle
            assert 'Rotate' in osd

        oc = OrientationConfidence(
            angle=angle,
            confidence=float(osd.get('Orientation confidence', 0)))
        return oc


def tesseract_log_output(log, stdout, input_file):
    lines = stdout.splitlines()
    prefix = "{0:4d}: [tesseract] ".format(page_number(input_file))
    for line in lines:
        if line.startswith("Tesseract Open Source"):
            continue
        elif line.startswith("Warning in pixReadMem"):
            continue
        elif 'diacritics' in line:
            log.warning(prefix + "lots of diacritics - possibly poor OCR")
        elif line.startswith('OSD: Weak margin'):
            log.warning(prefix + "unsure about page orientation")
        elif 'error' in line.lower() or 'exception' in line.lower():
            log.error(prefix + line.strip())
        elif 'warning' in line.lower():
            log.warning(prefix + line.strip())
        elif 'read_params_file' in line.lower():
            log.error(prefix + line.strip())
        else:
            log.info(prefix + line.strip())


def page_timedout(log, input_file):
    prefix = "{0:4d}: [tesseract] ".format(page_number(input_file))
    log.warning(prefix + " took too long to OCR - skipping")


def _generate_null_hocr(output_hocr, image):
    """Produce a .hocr file that reports no text detected on a page that is
    the same size as the input image."""
    from PIL import Image

    im = Image.open(image)
    w, h = im.size

    with open(output_hocr, 'w', encoding="utf-8") as f:
        f.write(HOCR_TEMPLATE.format(w, h))


def generate_hocr(input_file, output_hocr, language: list, engine_mode,
                  tessconfig: list,
                  timeout: float, pagesegmode: int, log):

    badxml = os.path.splitext(output_hocr)[0] + '.badxml'

    args_tesseract = tess_base_args(language, engine_mode)

    if pagesegmode is not None:
        args_tesseract.extend([psm(), str(pagesegmode)])

    args_tesseract.extend([
        input_file,
        badxml,
        'hocr'
    ] + tessconfig)
    try:
        log.debug(args_tesseract)
        stdout = check_output(
            args_tesseract, close_fds=True, stderr=STDOUT,
            universal_newlines=True, timeout=timeout)
    except TimeoutExpired:
        # Generate a HOCR file with no recognized text if tesseract times out
        # Temporary workaround to hocrTransform not being able to function if
        # it does not have a valid hOCR file.
        page_timedout(log, input_file)
        _generate_null_hocr(output_hocr, input_file)
    except CalledProcessError as e:
        tesseract_log_output(log, e.output, input_file)
        if 'read_params_file: parameter not found' in e.output:
            raise TesseractConfigError() from e
        if 'Image too large' in e.output:
            _generate_null_hocr(output_hocr, input_file)
            return

        raise e from e
    else:
        tesseract_log_output(log, stdout, input_file)

        if os.path.exists(badxml + '.html'):
            # Tesseract 3.02 appends suffix ".html" on its own (.badxml.html)
            shutil.move(badxml + '.html', badxml)
        elif os.path.exists(badxml + '.hocr'):
            # Tesseract 3.03 appends suffix ".hocr" on its own (.badxml.hocr)
            shutil.move(badxml + '.hocr', badxml)

        # Tesseract 3.03 inserts source filename into hocr file without
        # escaping it, creating invalid XML and breaking the parser.
        # As a workaround, rewrite the hocr file, replacing the filename
        # with a space.  Don't know if Tesseract 3.02 does the same.

        regex_nested_single_quotes = re.compile(
            r"""title='image "([^"]*)";""")
        with open(badxml, mode='r', encoding='utf-8') as f_in, \
                open(output_hocr, mode='w', encoding='utf-8') as f_out:
            for line in f_in:
                line = regex_nested_single_quotes.sub(
                    r"""title='image " ";""", line)
                f_out.write(line)


def use_skip_page(text_only, skip_pdf, output_pdf):
    if not text_only:
        os.symlink(skip_pdf, output_pdf)
        return

    # For text only we must create a blank page with dimensions identical
    # to the skip page because this is equivalent to a page with no text

    pdf_in = pypdf.PdfFileReader(skip_pdf)
    page0 = pdf_in.pages[0]

    with open(output_pdf, 'wb') as out:
        pdf_out = pypdf.PdfFileWriter()
        w, h = page0.mediaBox.getWidth(), page0.mediaBox.getHeight()
        pdf_out.addBlankPage(w, h)
        pdf_out.write(out)


def generate_pdf(input_image, skip_pdf, output_pdf, language: list,
                 engine_mode, text_only: bool,
                 tessconfig: list, timeout: float, pagesegmode: int, log):
    '''Use Tesseract to render a PDF.

    input_image -- image to analyze
    skip_pdf -- if we time out, use this file as output
    output_pdf -- file to generate
    language -- list of languages to consider
    engine_mode -- engine mode argument for tess v4
    text_only -- enable tesseract text only mode?
    tessconfig -- tesseract configuration
    timeout -- timeout (seconds)
    log -- logger object
    '''

    args_tesseract = tess_base_args(language, engine_mode)

    if pagesegmode is not None:
        args_tesseract.extend([psm(), str(pagesegmode)])

    if text_only:
        args_tesseract.extend(['-c', 'textonly_pdf=1'])

    input_image_clean = input_image + '-clean.png'
    log.info('Textcleaning ' + input_image)
    os.system('textcleaner -g -e stretch -f 25 -o 10 -u ' + input_image + ' ' + input_image_clean)
    os.rename(input_image_clean, input_image)


    args_tesseract.extend([
        input_image,
        os.path.splitext(output_pdf)[0],  # Tesseract appends suffix
        'pdf'
    ] + tessconfig)

    try:
        log.debug(args_tesseract)
        stdout = check_output(
            args_tesseract, close_fds=True, stderr=STDOUT,
            universal_newlines=True, timeout=timeout)
    except TimeoutExpired:
        page_timedout(log, input_image)
        use_skip_page(text_only, skip_pdf, output_pdf)
    except CalledProcessError as e:
        tesseract_log_output(log, e.output, input_image)
        if 'read_params_file: parameter not found' in e.output:
            raise TesseractConfigError() from e

        if 'Image too large' in e.output:
            use_skip_page(text_only, skip_pdf, output_pdf)
            return
        raise e from e
    else:
        tesseract_log_output(log, stdout, input_image)
