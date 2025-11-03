import sys, math, pygame
from pygame.locals import *
from OpenGL.GL import *
from OpenGL.GLU import *
from PIL import Image
import pillow_heif  # ensure pillow-heif is installed


def load_texture(image_path):
    pillow_heif.register_heif_opener()
    img = Image.open(image_path)
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    img_data = img.convert("RGB").tobytes()
    width, height = img.size

    texture_id = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, texture_id)
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, width, height, 0, GL_RGB, GL_UNSIGNED_BYTE, img_data)
    return texture_id


def init_gl(width, height):
    glEnable(GL_TEXTURE_2D)
    glClearColor(0.0, 0.0, 0.0, 1.0)
    glEnable(GL_DEPTH_TEST)
    glMatrixMode(GL_PROJECTION)
    gluPerspective(45, (width / height), 0.1, 1000.0)
    glMatrixMode(GL_MODELVIEW)


def sphere_viewer(image_path):
    pygame.init()
    screen = pygame.display.set_mode((1920, 1080), DOUBLEBUF | OPENGL)
    pygame.display.set_caption("Photosphere Viewer")
    init_gl(1920, 1080)
    texture = load_texture(image_path)

    quadric = gluNewQuadric()
    gluQuadricTexture(quadric, GL_TRUE)
    gluQuadricOrientation(quadric, GLU_INSIDE)

    clock = pygame.time.Clock()
    # Initialize rotation about each axis
    rot_x, rot_y, rot_z = 0.0, 0.0, 0.0
    rot_speed = 1.0

    while True:
        for event in pygame.event.get():
            if event.type == QUIT:
                pygame.quit()
                sys.exit()

        # Handle continuous key presses:
        keys = pygame.key.get_pressed()
        if keys[K_w]:
            rot_x += rot_speed
        if keys[K_s]:
            rot_x -= rot_speed
        if keys[K_q]:
            rot_y += rot_speed
        if keys[K_e]:
            rot_y -= rot_speed
        if keys[K_a]:
            rot_z += rot_speed
        if keys[K_d]:
            rot_z -= rot_speed

        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        # Apply rotations in order: X, Y, Z
        glRotatef(rot_x, 1, 0, 0)
        glRotatef(rot_y, 0, 1, 0)
        glRotatef(rot_z, 0, 0, 1)

        gluSphere(quadric, 80, 60, 60)

        pygame.display.flip()
        clock.tick(60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="View a photosphere panorama using a 3D sphere."
    )
    sphere_viewer("panorama_test.jpg")
